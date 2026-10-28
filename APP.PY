import os
import sqlite3
import asyncio
import logging
import threading

from flask import Flask, request
from telegram import Update
from telegram.error import BadRequest
from telegram.ext import Application, CommandHandler, MessageHandler, filters
from google import genai
from google.genai import types, errors

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")

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


class CuotaAgotada(Exception):
    """Ningún modelo de la lista pudo responder."""


async def generar(contenido):
    """Prueba cada modelo en orden. Salta al siguiente si hay cuota agotada (429),
    modelo inexistente (404) o servidor sobrecargado (500/503)."""
    ultimo_error = None
    for modelo in MODELOS:
        try:
            respuesta = await gemini_client.aio.models.generate_content(
                model=modelo,
                contents=contenido,
                config=types.GenerateContentConfig(system_instruction=PROMPT_SISTEMA),
            )
            if modelo != MODELOS[0]:
                log.info("Respondió el modelo de respaldo: %s", modelo)
            return respuesta
        except (errors.ClientError, errors.ServerError) as e:
            if e.code in (429, 404, 500, 503):
                log.warning("Modelo %s no disponible (%s); probando el siguiente", modelo, e.code)
                ultimo_error = e
                continue
            raise  # 400/403 (API key inválida, etc.) no se arreglan cambiando de modelo
    raise CuotaAgotada() from ultimo_error


async def start(update: Update, context):
    await update.message.reply_text(
        "¡Hola! Soy tu agente inteligente operando 24/7 en la nube 🚀\n"
        "Recuerdo nuestra conversación. Usa /reset si quieres que la olvide."
    )


async def reset(update: Update, context):
    borrar_historial(update.effective_chat.id)
    await update.message.reply_text("Listo, borré la memoria de esta conversación 🧹")


async def responder(update: Update, context):
    chat_id = update.effective_chat.id
    texto_usuario = update.message.text
    await context.bot.send_chat_action(chat_id, "typing")

    try:
        contenido = cargar_historial(chat_id) + [
            types.Content(role="user", parts=[types.Part(text=texto_usuario)])
        ]
        respuesta = await generar(contenido)
        texto = respuesta.text or "No pude generar una respuesta."
        # Solo se guarda si Gemini respondió bien, así el historial no queda cojo
        if respuesta.text:
            guardar_turno(chat_id, texto_usuario, texto)
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
telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, responder))


async def _iniciar():
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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
