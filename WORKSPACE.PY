"""Herramientas de Google Workspace (Sheets, Docs, Drive, Calendar) para el bot.

Se conectan a Gemini mediante function calling. Usan una cuenta de servicio:
solo pueden ver y editar lo que compartas con su correo (client_email del JSON).
No incluyen ninguna función para borrar ni para crear archivos nuevos.
"""
import os
import json
import asyncio
import logging
import functools
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

log = logging.getLogger("workspace")

ZONA = "America/Santiago"
# Correo del calendario donde crear y leer eventos (compártelo con la cuenta de servicio)
CALENDAR_ID = os.environ.get("CALENDAR_ID", "primary")
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/calendar",
]

_creds = None
_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
if _json:
    try:
        _creds = service_account.Credentials.from_service_account_info(
            json.loads(_json), scopes=SCOPES
        )
        log.info("Google Workspace activado (%s)", _creds.service_account_email)
    except Exception:
        log.exception("GOOGLE_CREDENTIALS_JSON inválido; Workspace desactivado")
else:
    log.info("Sin GOOGLE_CREDENTIALS_JSON: Workspace desactivado")


def _servicio(api, version):
    # Se crea uno por llamada: los clientes de googleapiclient no son thread-safe
    return build(api, version, credentials=_creds, cache_discovery=False)


def _herramienta(fn):
    """Corre la función bloqueante en un hilo y devuelve los errores como datos,
    para que Gemini pueda explicárselos al usuario en vez de romper la conversación."""

    @functools.wraps(fn)
    async def envoltura(*args, **kwargs):
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except HttpError as e:
            log.warning("Error de Google API en %s: %s", fn.__name__, e)
            return {"error": f"Google respondió con error {e.resp.status}: {e._get_reason()}"}
        except Exception as e:
            log.exception("Error en %s", fn.__name__)
            return {"error": str(e)}

    return envoltura


# ---------------------------------------------------------------- Drive
@_herramienta
def buscar_archivos(nombre: str = "") -> dict:
    """Busca archivos de Google Drive compartidos con el bot (Sheets, Docs, carpetas, etc.).
    Si 'nombre' está vacío, lista los más recientes. Devuelve id, nombre y tipo de cada archivo."""
    q = "trashed = false"
    if nombre:
        q += " and name contains '" + nombre.replace("\\", "\\\\").replace("'", "\\'") + "'"
    r = (
        _servicio("drive", "v3")
        .files()
        .list(
            q=q,
            pageSize=20,
            orderBy="modifiedTime desc",
            fields="files(id,name,mimeType,modifiedTime)",
            includeItemsFromAllDrives=True,
            supportsAllDrives=True,
        )
        .execute()
    )
    return {"archivos": r.get("files", [])}


# ---------------------------------------------------------------- Sheets
@_herramienta
def ver_hojas(spreadsheet_id: str) -> dict:
    """Devuelve los nombres de las pestañas de una hoja de cálculo de Google Sheets."""
    r = (
        _servicio("sheets", "v4")
        .spreadsheets()
        .get(spreadsheetId=spreadsheet_id, fields="properties.title,sheets.properties.title")
        .execute()
    )
    return {
        "titulo": r["properties"]["title"],
        "pestanas": [s["properties"]["title"] for s in r.get("sheets", [])],
    }


@_herramienta
def leer_hoja(spreadsheet_id: str, rango: str) -> dict:
    """Lee celdas de Google Sheets. 'rango' en notación A1, por ejemplo 'Ventas!A1:E50'
    o solo el nombre de la pestaña para leerla completa."""
    r = (
        _servicio("sheets", "v4")
        .spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=rango)
        .execute()
    )
    valores = r.get("values", [])
    return {"valores": valores[:200], "recortado": len(valores) > 200}


@_herramienta
def agregar_filas(spreadsheet_id: str, pestana: str, filas: list[list[str]]) -> dict:
    """Agrega una o más filas al final de una pestaña de Google Sheets, después de la
    última fila con datos. Cada fila es una lista de valores (texto, números o fechas)."""
    r = (
        _servicio("sheets", "v4")
        .spreadsheets()
        .values()
        .append(
            spreadsheetId=spreadsheet_id,
            range=pestana,
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": filas},
        )
        .execute()
    )
    return {"ok": True, "rango_actualizado": r.get("updates", {}).get("updatedRange")}


@_herramienta
def actualizar_celdas(spreadsheet_id: str, rango: str, valores: list[list[str]]) -> dict:
    """SOBRESCRIBE celdas existentes de Google Sheets en el rango indicado (notación A1,
    por ejemplo 'Ventas!B2:C3'). Pide confirmación al usuario antes de usarla."""
    r = (
        _servicio("sheets", "v4")
        .spreadsheets()
        .values()
        .update(
            spreadsheetId=spreadsheet_id,
            range=rango,
            valueInputOption="USER_ENTERED",
            body={"values": valores},
        )
        .execute()
    )
    return {"ok": True, "celdas_actualizadas": r.get("updatedCells")}


# ---------------------------------------------------------------- Docs
@_herramienta
def leer_documento(document_id: str) -> dict:
    """Devuelve el texto de un documento de Google Docs."""
    doc = _servicio("docs", "v1").documents().get(documentId=document_id).execute()
    texto = []
    for bloque in doc.get("body", {}).get("content", []):
        for el in bloque.get("paragraph", {}).get("elements", []):
            texto.append(el.get("textRun", {}).get("content", ""))
    completo = "".join(texto)
    return {"titulo": doc.get("title"), "texto": completo[:20000], "recortado": len(completo) > 20000}


@_herramienta
def agregar_texto_documento(document_id: str, texto: str) -> dict:
    """Agrega texto como un párrafo nuevo al final de un documento de Google Docs."""
    _servicio("docs", "v1").documents().batchUpdate(
        documentId=document_id,
        body={
            "requests": [
                {"insertText": {"endOfSegmentLocation": {}, "text": "\n" + texto}}
            ]
        },
    ).execute()
    return {"ok": True}


@_herramienta
def reemplazar_texto_documento(document_id: str, buscar: str, reemplazar: str) -> dict:
    """Reemplaza TODAS las apariciones de un texto por otro en un documento de Google Docs
    (distingue mayúsculas). Pide confirmación al usuario antes de usarla."""
    r = (
        _servicio("docs", "v1")
        .documents()
        .batchUpdate(
            documentId=document_id,
            body={
                "requests": [
                    {
                        "replaceAllText": {
                            "containsText": {"text": buscar, "matchCase": True},
                            "replaceText": reemplazar,
                        }
                    }
                ]
            },
        )
        .execute()
    )
    cambios = r["replies"][0].get("replaceAllText", {}).get("occurrencesChanged", 0)
    return {"ok": True, "reemplazos": cambios}


# ---------------------------------------------------------------- Calendar
@_herramienta
def listar_eventos(dias: int = 7) -> dict:
    """Lista los eventos del calendario desde ahora hasta dentro de 'dias' días."""
    tz = ZoneInfo(ZONA)
    ahora = datetime.now(tz)
    r = (
        _servicio("calendar", "v3")
        .events()
        .list(
            calendarId=CALENDAR_ID,
            timeMin=ahora.isoformat(),
            timeMax=(ahora + timedelta(days=dias)).isoformat(),
            singleEvents=True,
            orderBy="startTime",
            maxResults=50,
        )
        .execute()
    )
    return {
        "eventos": [
            {
                "id": e["id"],
                "titulo": e.get("summary", "(sin título)"),
                "inicio": e["start"].get("dateTime", e["start"].get("date")),
                "fin": e["end"].get("dateTime", e["end"].get("date")),
            }
            for e in r.get("items", [])
        ]
    }


@_herramienta
def crear_evento(
    titulo: str, inicio: str, fin: str, descripcion: str = "", aviso_minutos: int = 10
) -> dict:
    """Crea un evento en el calendario con un aviso en el celular 'aviso_minutos' minutos antes.
    'inicio' y 'fin' en formato ISO sin zona horaria, hora de Chile, por ejemplo
    '2026-10-28T15:00:00'."""
    r = (
        _servicio("calendar", "v3")
        .events()
        .insert(
            calendarId=CALENDAR_ID,
            body={
                "summary": titulo,
                "description": descripcion,
                "start": {"dateTime": inicio, "timeZone": ZONA},
                "end": {"dateTime": fin, "timeZone": ZONA},
                "reminders": {
                    "useDefault": False,
                    "overrides": [{"method": "popup", "minutes": aviso_minutos}],
                },
            },
        )
        .execute()
    )
    return {"ok": True, "id": r.get("id"), "enlace": r.get("htmlLink")}


HERRAMIENTAS = (
    [
        buscar_archivos,
        ver_hojas,
        leer_hoja,
        agregar_filas,
        actualizar_celdas,
        leer_documento,
        agregar_texto_documento,
        reemplazar_texto_documento,
        listar_eventos,
        crear_evento,
    ]
    if _creds
    else []
)
