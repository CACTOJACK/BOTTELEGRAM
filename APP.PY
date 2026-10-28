import os
import sqlite3
import asyncio
import logging
import time
import threading
from contextvars import ContextVar
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from flask import Flask, request
from telegram import Update
from telegram.error import BadRequest
from telegram.ext import Application, CommandHandler, MessageHandler, filters
from google import genai
from google.genai import types, errors

from WORKSPACE import HERRAMIENTAS  # lista vacía si no hay credenciales de Google

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")

# Sirve para comprobar qué versión está desplegada en Render (se ve con /fecha y en los logs)
VERSION = "2026-10-27-fecha"

# --- Credenciales (variables de entorno en Render) ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
# Modelos en orden de preferencia: si uno falla por cuota, se prueba el siguiente.
# Puedes cambiarlos con la variable GEMINI_MODELS (separados por comas).
MODELOS = [
    m.strip()
    for m in os.environ.get(
        "GEMINI_MODELS",
        "gemini-3.8-flash,gemini-3.7-flash,gemini-3.5-flash-lite,gemini-2.5-flash",
    ).split(",")
    if m.strip()
]
# Render define esta variable automáticamente (https://tu-servicio.onrender.com)
BASE_URL = os.environ.get("RENDER_EXTERNAL_URL")

# --- Memoria ---
DB_PATH = os.environ.get("DB_PATH", "memoria.db")
# Cantidad de mensajes recordados por chat (usa un número par: pregunta + respuesta)
MAX_MENSAJES = int(os.environ.get("MAX_MENSAJES", "30"))

# --- Recordatorios ---
# Clave del endpoint /tick que llama cron-job.org cada minuto (defínela en Render)
TICK_KEY = os.environ.get("TICK_KEY")
TZ = ZoneInfo("America/Santiago")

if not TELEGRAM_TOKEN or not GEMINI_API_KEY:
    raise ValueError("Faltan las variables de entorno TELEGRAM_TOKEN o GEMINI_API_KEY.")

PROMPT_SISTEMA = (
    "Eres un asistente personal eficiente, directo, amigable y brillante "
    "resolviendo dudas y organizando tareas. Recuerdas lo conversado antes "
    "con la persona y lo usas cuando es relevante."
)

gemini_client = genai.Client(api_key=GEMINI_API_KEY)
app = Flask(__name__)

# --- Base de datos de historial ---
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.execute(
    """CREATE TABLE IF NOT EXISTS historial (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER NOT NULL,
        rol TEXT NOT NULL,
        texto TEXT NOT NULL
    )"""
)
db.execute("CREATE INDEX IF NOT EXISTS idx_chat ON historial(chat_id, id)")
db.execute(
    """CREATE TABLE IF NOT EXISTS recordatorios (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER NOT NULL,
        texto TEXT NOT NULL,
        fecha_hora TEXT NOT NULL,          -- hora de Chile, 'YYYY-MM-DD HH:MM:SS'
        enviado INTEGER NOT NULL DEFAULT 0
    )"""
)
db.commit()


def cargar_historial(chat_id):
    """Devuelve los últimos MAX_MENSAJES mensajes del chat, en orden cronológico."""
    filas = db.execute(
        """SELECT rol, texto FROM (
               SELECT id, rol, texto FROM historial
               WHERE chat_id = ? ORDER BY id DESC LIMIT ?
           ) ORDER BY id ASC""",
        (chat_id, MAX_MENSAJES),
    ).fetchall()
    return [types.Content(role=rol, parts=[types.Part(text=texto)]) for rol, texto in filas]


def guardar_turno(chat_id, pregunta, respuesta):
    """Guarda pregunta y respuesta juntas y recorta lo más antiguo."""
    db.executemany(
        "INSERT INTO historial (chat_id, rol, texto) VALUES (?, ?, ?)",
        [(chat_id, "user", pregunta), (chat_id, "model", respuesta)],
    )
    db.execute(
        """DELETE FROM historial WHERE chat_id = ? AND id NOT IN (
               SELECT id FROM historial WHERE chat_id = ? ORDER BY id DESC LIMIT ?
           )""",
        (chat_id, chat_id, MAX_MENSAJES),
    )
    db.commit()


def borrar_historial(chat_id):
    db.execute("DELETE FROM historial WHERE chat_id = ?", (chat_id,))
    db.commit()


# --- Un único event loop persistente en un hilo aparte ---
loop = asyncio.new_event_loop()
threading.Thread(target=loop.run_forever, daemon=True).start()

telegram_app = Application.builder().token(TELEGRAM_TOKEN).build()


# --- Herramientas de recordatorios (funcionan sin Google) ---
chat_actual = ContextVar("chat_actual", default=None)  # chat que está conversando ahora
_tick_en_curso = False
FORMATO_DB = "%Y-%m-%d %H:%M:%S"


def _ahora_chile():
    return datetime.now(TZ).replace(tzinfo=None)


async def crear_recordatorio(texto: str, fecha_hora: str) -> dict:
    """Programa un recordatorio: el bot le escribirá al usuario por Telegram a esa hora.
    'fecha_hora' en hora de Chile, formato ISO sin zona horaria, por ejemplo
    '2026-10-28T09:00:00'. 'texto' es lo que se le debe recordar."""
    chat_id = chat_actual.get()
    if chat_id is None:
        return {"error": "No sé a qué chat enviar el recordatorio."}
    try:
        momento = datetime.fromisoformat(fecha_hora)
    except ValueError:
        return {"error": "Fecha inválida; usa el formato 2026-10-28T09:00:00."}
    if momento.tzinfo is not None:
        momento = momento.astimezone(TZ).replace(tzinfo=None)
    if momento < _ahora_chile() - timedelta(minutes=1):
        return {"error": "Esa fecha y hora ya pasó. Pregúntale al usuario para cuándo lo quiere."}
    cur = db.execute(
        "INSERT INTO recordatorios (chat_id, texto, fecha_hora) VALUES (?, ?, ?)",
        (chat_id, texto, momento.strftime(FORMATO_DB)),
    )
    db.commit()
    return {"ok": True, "id": cur.lastrowid, "para": momento.strftime("%d-%m-%Y %H:%M")}


async def listar_recordatorios() -> dict:
    """Lista los recordatorios pendientes (aún no enviados) del usuario."""
    filas = db.execute(
        "SELECT id, texto, fecha_hora FROM recordatorios "
        "WHERE chat_id = ? AND enviado = 0 ORDER BY fecha_hora",
        (chat_actual.get(),),
    ).fetchall()
    return {"pendientes": [{"id": i, "texto": t, "para": f} for i, t, f in filas]}


async def cancelar_recordatorio(recordatorio_id: int) -> dict:
    """Cancela un recordatorio pendiente usando su id (obtenlo con listar_recordatorios)."""
    cur = db.execute(
        "DELETE FROM recordatorios WHERE id = ? AND chat_id = ? AND enviado = 0",
        (recordatorio_id, chat_actual.get()),
    )
    db.commit()
    return {"ok": cur.rowcount > 0}


async def fecha_hora_actual() -> dict:
    """Devuelve la fecha y hora reales de hoy en Chile. Úsala siempre que te pregunten qué
    día, fecha, año u hora es, o cuando necesites calcular fechas relativas."""
    return {"fecha_hora": fecha_actual_texto(), "iso": _ahora_chile().isoformat(timespec="minutes")}


HERRAMIENTAS_BOT = HERRAMIENTAS + [
    fecha_hora_actual,
    crear_recordatorio,
    listar_recordatorios,
    cancelar_recordatorio,
]


async def procesar_recordatorios():
    """Envía los recordatorios vencidos. Lo dispara /tick (cron-job.org, cada minuto)."""
    global _tick_en_curso
    if _tick_en_curso:  # evita enviar duplicados si dos llamadas se solapan
        return
    _tick_en_curso = True
    try:
        ahora = _ahora_chile().strftime(FORMATO_DB)
        filas = db.execute(
            "SELECT id, chat_id, texto FROM recordatorios "
            "WHERE enviado = 0 AND fecha_hora <= ? ORDER BY fecha_hora",
            (ahora,),
        ).fetchall()
        for id_, chat_id, texto in filas:
            try:
                await telegram_app.bot.send_message(chat_id, f"⏰ Recordatorio: {texto}")
                db.execute("UPDATE recordatorios SET enviado = 1 WHERE id = ?", (id_,))
                db.commit()
            except Exception:
                log.exception("No pude enviar el recordatorio %s; se reintenta en el próximo tick", id_)
    finally:
        _tick_en_curso = False


INSTRUCCIONES_RECORDATORIOS = (
    "\n\nPuedes programar recordatorios con crear_recordatorio: el usuario los recibirá por "
    "Telegram a esa hora. Convierte expresiones como 'mañana a las 9' a una fecha ISO usando "
    "la fecha y hora actual de Chile. Si falta el día o la hora, pregúntalo. Confirma qué "
    "recordarás y para cuándo. Usa listar_recordatorios y cancelar_recordatorio cuando lo pida."
)

INSTRUCCIONES_WORKSPACE = (
    "\n\nTienes herramientas de Google Workspace (Drive, Sheets, Docs, Calendar). "
    "Cuando el usuario mencione un archivo por nombre, búscalo con buscar_archivos para "
    "obtener su id; antes de escribir en una hoja, mira sus pestañas con ver_hojas y, si "
    "hace falta, lee su encabezado para respetar el orden de las columnas. Agregar filas o "
    "texto es seguro; antes de SOBRESCRIBIR celdas o REEMPLAZAR texto existente, pide "
    "confirmación al usuario. Si falta un dato para guardar, pregúntalo. Al terminar, "
    "indica con claridad qué guardaste y dónde (archivo, pestaña y valores). Si una "
    "herramienta devuelve un error, explícalo en palabras simples."
)


DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MESES = [
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
]


def fecha_actual_texto():
    """Fecha y hora real de Chile, en español (el servidor de Render trabaja en UTC)."""
    ahora = datetime.now(TZ)
    return (
        f"{DIAS[ahora.weekday()]} {ahora.day} de {MESES[ahora.month - 1]} de {ahora.year}, "
        f"{ahora:%H:%M} (hora de Chile)"
    )


def armar_prompt():
    prompt = (
        f"FECHA Y HORA ACTUAL: {fecha_actual_texto()}. Esta es la fecha real de hoy: úsala "
        "siempre y descarta cualquier otra fecha o año que creas conocer o que aparezca en "
        "mensajes anteriores de la conversación. Si te preguntan qué día, fecha, año u hora "
        "es, responde con esa fecha o llama a fecha_hora_actual.\n\n" + PROMPT_SISTEMA
    )
    prompt += INSTRUCCIONES_RECORDATORIOS
    if HERRAMIENTAS:
        prompt += INSTRUCCIONES_WORKSPACE
    return prompt


COOLDOWN_SEG = int(os.environ.get("COOLDOWN_SEG", "900"))  # 15 min por defecto
agotados = {}  # modelo -> momento (epoch) hasta el cual se salta


class CuotaAgotada(Exception):
    """Ningún modelo de la lista pudo responder."""


async def generar(contenido):
    """Prueba cada modelo en orden. Salta al siguiente si hay cuota agotada (429),
    modelo inexistente (404) o servidor sobrecargado (500/503)."""
    ultimo_error = None
    ahora = time.time()
    # Se saltan los modelos que fallaron hace poco, para no perder tiempo reintentándolos.
    # Si todos están en pausa, se intenta con la lista completa.
    candidatos = [m for m in MODELOS if agotados.get(m, 0) <= ahora] or MODELOS
    for modelo in candidatos:
        try:
            respuesta = await gemini_client.aio.models.generate_content(
                model=modelo,
                contents=contenido,
                config=types.GenerateContentConfig(
                    system_instruction=armar_prompt(),
                    tools=HERRAMIENTAS_BOT,
                ),
            )
            if modelo != MODELOS[0]:
                log.info("Respondió el modelo de respaldo: %s", modelo)
            return respuesta
        except (errors.ClientError, errors.ServerError) as e:
            if e.code in (429, 404, 500, 503):
                log.warning("Modelo %s no disponible (%s); probando el siguiente", modelo, e.code)
                agotados[modelo] = time.time() + COOLDOWN_SEG
                ultimo_error = e
                continue
            raise  # 400/403 (API key inválida, etc.) no se arreglan cambiando de modelo
    raise CuotaAgotada() from ultimo_error


async def start(update: Update, context):
    await update.message.reply_text(
        "¡Hola! Soy tu agente inteligente operando 24/7 en la nube 🚀\n"
        "Recuerdo nuestra conversación. Usa /reset si quieres que la olvide."
    )


async def comando_fecha(update: Update, context):
    """Responde sin usar Gemini: sirve para comprobar el deploy y la hora del servidor."""
    await update.message.reply_text(f"Versión: {VERSION}\n{fecha_actual_texto()}")


async def reset(update: Update, context):
    borrar_historial(update.effective_chat.id)
    await update.message.reply_text("Listo, borré la memoria de esta conversación 🧹")


async def responder(update: Update, context):
    texto_usuario = update.message.text
    await atender(update, context, [types.Part(text=texto_usuario)], texto_usuario)


async def responder_audio(update: Update, context):
    """Mensajes de voz o archivos de audio: Gemini los entiende directamente."""
    msg = update.message
    media = msg.voice or msg.audio
    mime = getattr(media, "mime_type", None) or "audio/ogg"

    await context.bot.send_chat_action(update.effective_chat.id, "typing")
    try:
        archivo = await context.bot.get_file(media.file_id)
        datos = bytes(await archivo.download_as_bytearray())
    except Exception:
        log.exception("ERROR descargando el audio")
        await msg.reply_text("No pude descargar tu audio, ¿lo intentas de nuevo?")
        return

    instruccion = (
        "El usuario te envió este mensaje de audio. Empieza tu respuesta con una línea "
        "'🎤 Entendí: ' seguida de una transcripción breve de lo que dijo, y luego "
        "responde o haz lo que pida."
    )
    partes = [types.Part.from_bytes(data=datos, mime_type=mime), types.Part(text=instruccion)]
    # El audio no se guarda en el historial; la transcripción queda en la respuesta del bot
    await atender(update, context, partes, "[Mensaje de voz]")


async def atender(update: Update, context, partes, texto_historial):
    chat_id = update.effective_chat.id
    chat_actual.set(chat_id)  # las herramientas de recordatorios lo leen
    await context.bot.send_chat_action(chat_id, "typing")

    try:
        # La fecha va también en el mensaje actual (sin guardarla en el historial): el modelo
        # le da más peso que al prompt de sistema y no arrastra fechas viejas de la memoria.
        sello = types.Part(text=f"[Fecha y hora actual: {fecha_actual_texto()}]")
        contenido = cargar_historial(chat_id) + [types.Content(role="user", parts=[sello] + partes)]
        respuesta = await generar(contenido)
        texto = respuesta.text or "No pude generar una respuesta."
        # Solo se guarda si Gemini respondió bien, así el historial no queda cojo
        if respuesta.text:
            guardar_turno(chat_id, texto_historial, texto)
    except CuotaAgotada:
        log.error("Todos los modelos agotaron su cuota o no están disponibles")
        await update.message.reply_text(
            "Se agotó la cuota diaria de todos mis modelos. Prueba de nuevo más tarde 🙏"
        )
        return
    except Exception:
        log.exception("ERROR REAL DE GEMINI")
        await update.message.reply_text("Tuve un pequeño problema procesando tu mensaje.")
        return

    # Telegram permite máx. 4096 caracteres por mensaje
    for i in range(0, len(texto), 4000):
        trozo = texto[i : i + 4000]
        try:
            await update.message.reply_text(trozo, parse_mode="Markdown")
        except BadRequest:
            # Markdown inválido: enviar como texto plano
            await update.message.reply_text(trozo)


telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(CommandHandler("reset", reset))
telegram_app.add_handler(CommandHandler("fecha", comando_fecha))
telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, responder))
telegram_app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, responder_audio))


async def _iniciar():
    log.info("Iniciando bot, versión %s", VERSION)
    await telegram_app.initialize()
    if BASE_URL:
        await telegram_app.bot.set_webhook(f"{BASE_URL}/{TELEGRAM_TOKEN}")
        log.info("Webhook configurado en %s", BASE_URL)


asyncio.run_coroutine_threadsafe(_iniciar(), loop).result()


@app.route("/")
def home():
    return "¡El bot de Telegram con Gemini está activo y funcionando!"


@app.route(f"/{TELEGRAM_TOKEN}", methods=["POST"])
def webhook():
    try:
        update = Update.de_json(request.get_json(force=True), telegram_app.bot)
        asyncio.run_coroutine_threadsafe(telegram_app.process_update(update), loop)
    except Exception:
        log.exception("ERROR en webhook")
    return "OK", 200


@app.route("/tick")
def tick():
    """cron-job.org llama aquí cada minuto: despierta el servicio y envía recordatorios vencidos."""
    if TICK_KEY and request.args.get("key") != TICK_KEY:
        return "forbidden", 403
    asyncio.run_coroutine_threadsafe(procesar_recordatorios(), loop)
    return "OK", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
