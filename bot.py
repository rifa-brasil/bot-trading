import os
import json
import asyncio
from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, CallbackQueryHandler, MessageHandler, filters

# --- CONFIGURACIÓN ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
ADMIN_TELEGRAM_ID = int(os.environ.get("ADMIN_TELEGRAM_ID", "0"))
ADMIN_USERNAME = "@yordanisr"
DB_FILE = "inversion_db.json"
PAQUETES_DISPONIBLES = [100, 120, 150, 180, 200, 250, 300, 350, 500, 1000, 1500, 2000]

# --- SERVIDOR WEB ---
async def handle_web(request): return web.Response(text="Bot Activo")
async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_web)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.environ.get("PORT", 10000))).start()

# --- UTILIDADES ---
def obtener_data():
    if not os.path.exists(DB_FILE): return {"usuarios": {}, "estados_registro": {}, "estados_retiro": {}, "estados_admin_retiro": {}, "pendientes_referido": {}}
    return json.load(open(DB_FILE, "r"))

def guardar_data(data):
    with open(DB_FILE, "w") as f: json.dump(data, f, indent=4)

def obtener_menu(registrado):
    if not registrado:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("📝 Registrarse", callback_data="iniciar_registro")],
            [InlineKeyboardButton("ℹ️ Información / Reglas", callback_data="ver_info")]
        ])
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 Mi Saldo y Estadísticas", callback_data="ver_saldo")],
        [InlineKeyboardButton("📤 Solicitar Retiro", callback_data="pedir_retiro")],
        [InlineKeyboardButton("👥 Invitar Amigos", callback_data="ver_invitacion")],
        [InlineKeyboardButton("ℹ️ Información / Reglas", callback_data="ver_info")]
    ])

# --- HANDLERS ---
async def start_command(update, context):
    user_id = str(update.effective_user.id)
    data = obtener_data()
    is_reg = user_id in data["usuarios"]
    await update.message.reply_text("👋 ¡Bienvenido!", reply_markup=obtener_menu(is_reg))

async def boton_callback(update, context):
    query = update.callback_query
    await query.answer()
    data_cb = query.data
    user_id = str(query.from_user.id)
    data = obtener_data()

    if data_cb == "ver_info":
        await query.message.reply_text("ℹ️ **Reglas e Información**\n\n1. Rendimiento: 0.50% diario.\n2. Retiros: Jueves y Viernes.\n3. Mínimo retiro: 50 USDT.", parse_mode="Markdown", reply_markup=obtener_menu(user_id in data["usuarios"]))
    
    elif data_cb == "ver_invitacion":
        link = f"https://t.me/{context.bot.username}?start={user_id}"
        await query.message.reply_text(f"👥 Tu enlace: `{link}`", parse_mode="Markdown")

    elif data_cb == "ver_saldo":
        u = data["usuarios"].get(user_id, {})
        await query.message.reply_text(f"💰 Saldo: {u.get('ganancias_acumuladas', 0)} USDT", reply_markup=obtener_menu(True))

    elif data_cb == "pedir_retiro":
        data["estados_retiro"][user_id] = {"fase": "monto"}
        guardar_data(data)
        await query.message.reply_text("📤 Cantidad a retirar:")

    elif data_cb.startswith("ret_"):
        target_id = data_cb.split("_")[1]
        data["estados_admin_retiro"][user_id] = target_id
        guardar_data(data)
        await query.message.reply_text("📸 Envía el comprobante:")

    elif data_cb == "iniciar_registro":
        data["estados_registro"][user_id] = {"paso": 1}
        guardar_data(data)
        await query.message.reply_text("📝 Nombre:")

async def manejar_mensajes(update, context):
    user_id = str(update.effective_user.id)
    data = obtener_data()
    
    # Lógica de comprobante admin
    if user_id == ADMIN_TELEGRAM_ID and user_id in data.get("estados_admin_retiro", {}):
        if update.message.photo:
            target_id = data["estados_admin_retiro"].pop(user_id)
            guardar_data(data)
            await context.bot.send_photo(int(target_id), update.message.photo[-1].file_id, caption="✅ ¡Retiro aprobado!")
            await update.message.reply_text("✅ Enviado al usuario.")
    
    # (El resto de la lógica de registro/retiro permanece igual...)
    # Asegúrate de mantener tu lógica de registro aquí.

async def main():
    await start_web_server()
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CallbackQueryHandler(boton_callback))
    app.add_handler(MessageHandler(filters.ALL, manejar_mensajes))
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
