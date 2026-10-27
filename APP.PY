import os
import asyncio
from flask import Flask, request
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters
from google import genai

# Cargar credenciales de forma segura desde el entorno de la nube
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

if not TELEGRAM_TOKEN or not GEMINI_API_KEY:
    raise ValueError("Faltan las variables de entorno TELEGRAM_TOKEN o GEMINI_API_KEY.")

# Inicializar los clientes
gemini_client = genai.Client(api_key=GEMINI_API_KEY)
app = Flask(__name__)

# Configurar la aplicación del bot de Telegram
telegram_app = Application.builder().token(TELEGRAM_TOKEN).build()

# Comando /start
async def start(update: Update, context):
    await update.message.reply_text("¡Hola! Soy tu agente inteligente operando 24/7 en la nube 🚀")

# Manejador de mensajes de texto
async def responder(update: Update, context):
    texto_usuario = update.message.text
    try:
        prompt_sistema = (
            "Eres un asistente personal eficiente, directo, amigable y brillante "
            "resolviendo dudas y organizando tareas."
        )
        
        respuesta = gemini_client.models.generate_content(
            model="gemini-3.8-flash",
            contents=f"{prompt_sistema}\n\nMensaje del usuario: {texto_usuario}"
        )
        
        await update.message.reply_text(respuesta.text, parse_mode="Markdown")
    except Exception as e:
        print(f"Error generando respuesta: {e}")
        await update.message.reply_text("Tuve un pequeño problema procesando tu mensaje.")

# Registrar manejadores en el bot
telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, responder))

# Ruta base para verificar que el servidor web está vivo
@app.route("/")
def home():
    return "¡El bot de Telegram con Gemini está activo y funcionando!"

# Ruta del Webhook (Telegram enviará los mensajes aquí de forma automática)
@app.route(f"/{8925150491:AAFN98hJLK5bNoTmeTmVomCdDmcA9Bmn-mA}", methods=["POST"])
def webhook():
    json_data = request.get_json(force=True)
    update = Update.de_json(json_data, telegram_app.bot)
    
    # Ejecutar la actualización de forma asíncrona en Flask
    async def process():
        await telegram_app.initialize()
        await telegram_app.process_update(update)

    asyncio.run(process())
    return "ok"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
