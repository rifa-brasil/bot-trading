import os
import json
import asyncio
from datetime import datetime, timedelta
from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, CallbackQueryHandler, filters

# --- SERVIDOR WEB (NECESARIO PARA RENDER) ---
async def handle_web(request):
    return web.Response(text="Bot de Inversión Activo y en Línea 24/7!")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_web)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

# --- CONFIGURACIÓN ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
ADMIN_TELEGRAM_ID = int(os.environ.get("ADMIN_TELEGRAM_ID", "0"))
WALLET_EMPRESA = "TXXsV2612pJjoz6KrpfsPUqwGQryjGpRbE" 
ADMIN_USERNAME = "@yordanisr"

DB_FILE = "inversion_db.json"
PORCENTAJE_DIARIO = 0.50
MIN_RETIRO = 50.0

COMISION_RETIRO_GANANCIAS = 0.01
COMISION_RETIRO_DEPOSITO = 0.02
COMISION_REFERIDO = 0.02

PAQUETES_DISPONIBLES = [100, 120, 150, 180, 200, 250, 300, 350, 500, 1000, 1500, 2000]

def obtener_data():
    if not os.path.exists(DB_FILE):
        return {"usuarios": {}, "estados_registro": {}, "estados_retiro": {}, "estados_admin_retiro": {}, "pendientes_referido": {}}
    data = json.load(open(DB_FILE, "r"))
    if "estados_admin_retiro" not in data: data["estados_admin_retiro"] = {}
    if "pendientes_referido" not in data: data["pendientes_referido"] = {}
    return data

def guardar_data(data):
    with open(DB_FILE, "w") as f:
        json.dump(data, f, indent=4)

def obtener_menu(registrado: bool):
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

def obtener_teclado_paquetes():
    keyboard = []
    fila = []
    for i, monto in enumerate(PAQUETES_DISPONIBLES):
        fila.append(InlineKeyboardButton(f"💎 {monto} USDT", callback_data=f"paq_{monto}"))
        if len(fila) == 3:
            keyboard.append(fila)
            fila = []
    if fila: keyboard.append(fila)
    return InlineKeyboardMarkup(keyboard)

def es_misma_semana(fecha_str):
    if not fecha_str: return False
    try:
        fecha_retiro = datetime.strptime(fecha_str, "%Y-%m-%d").date()
        hoy = datetime.now().date()
        lunes_hoy = hoy - timedelta(days=hoy.weekday())
        lunes_retiro = fecha_retiro - timedelta(days=fecha_retiro.weekday())
        return lunes_hoy == lunes_retiro
    except: return False

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    data = obtener_data()
    if context.args and user_id not in data["usuarios"] and user_id not in data.get("estados_registro", {}):
        referidor_id = context.args[0]
        if referidor_id in data["usuarios"] and referidor_id != user_id:
            data["pendientes_referido"][user_id] = referidor_id
            guardar_data(data)
    is_reg = user_id in data["usuarios"]
    await update.message.reply_text("👋 ¡Bienvenido a la plataforma de inversión!" if not is_reg else "👋 ¡Bienvenido de nuevo a tu panel!", reply_markup=obtener_menu(is_reg))

async def manejar_mensajes_texto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    user_id = str(update.effective_user.id)
    data = obtener_data()
    texto = update.message.text.strip() if update.message.text else ""
    is_reg = user_id in data["usuarios"]

    if user_id == str(ADMIN_TELEGRAM_ID) and user_id in data.get("estados_admin_retiro", {}):
        target_id = data["estados_admin_retiro"][user_id]
        if update.message.photo:
            photo_file_id = update.message.photo[-1].file_id
            del data["estados_admin_retiro"][user_id]
            if target_id in data["usuarios"]: data["usuarios"][target_id]["ultimo_retiro_fecha"] = datetime.now().strftime("%Y-%m-%d")
            guardar_data(data)
            await update.message.reply_text("✅ Comprobante enviado con éxito.")
            try:
                await context.bot.send_photo(int(target_id), photo_file_id, caption="🎉 ¡Tu solicitud de retiro ha sido procesada con éxito!", reply_markup=obtener_menu(True))
            except: pass
            return
        else:
            await update.message.reply_text("⚠️ Adjunta la imagen del comprobante.")
            return

    if user_id in data.get("estados_registro", {}):
        estado_reg = data["estados_registro"][user_id]
        if estado_reg["paso"] == 1:
            estado_reg.update({"nombre": texto, "paso": 2})
            guardar_data(data)
            await update.message.reply_text("📧 Ingresa tu correo electrónico:")
        elif estado_reg["paso"] == 2:
            estado_reg.update({"email": texto, "paso": 3})
            guardar_data(data)
            await update.message.reply_text("📱 Ingresa tu teléfono:")
        elif estado_reg["paso"] == 3:
            estado_reg["telefono"] = texto
            guardar_data(data)
            await update.message.reply_text("💎 Selecciona el paquete de inversión:", reply_markup=obtener_teclado_paquetes())
        return

    if user_id in data.get("estados_retiro", {}):
        estado_ret = data["estados_retiro"][user_id]
        if estado_ret["fase"] == "monto":
            try:
                monto_solicitado = float(texto)
                user_info = data["usuarios"][user_id]
                ganancias_disp = user_info.get("ganancias_acumuladas", 0)
                deposito_total = user_info.get("deposito", 0)

                if ganancias_disp > 0 and monto_solicitado < MIN_RETIRO:
                    await update.message.reply_text(f"⚠️ Mínimo para retiro de ganancias: {MIN_RETIRO} USDT.")
                    return
                
                max_disponible = ganancias_disp if ganancias_disp > 0 else deposito_total
                if monto_solicitado > max_disponible:
                    await update.message.reply_text(f"⚠️ Saldo insuficiente. Máximo: {max_disponible:.2f} USDT.")
                    return

                tiene_ganancias = ganancias_disp >= monto_solicitado
                tasa_comision = COMISION_RETIRO_GANANCIAS if tiene_ganancias else COMISION_RETIRO_DEPOSITO
                comision_calculada = monto_solicitado * tasa_comision
                monto_neto = monto_solicitado - comision_calculada

                estado_ret.update({"monto": monto_solicitado, "fase": "wallet"})
                guardar_data(data)
                await update.message.reply_text(
                    f"📝 *Resumen de tu retiro:*\n\n💰 Solicitado: {monto_solicitado:.2f} USDT\n📉 Comisión ({int(tasa_comision * 100)}%): {comision_calculada:.2f} USDT\n💵 Neto a recibir: {monto_neto:.2f} USDT\n\nEscribe tu **wallet TRC20**:",
                    parse_mode="Markdown"
                )
            except ValueError: await update.message.reply_text("⚠️ Ingresa un número válido.")
            return

        elif estado_ret["fase"] == "wallet":
            wallet = texto
            monto_solicitado = estado_ret["monto"]
            user_info = data["usuarios"][user_id]
            tiene_ganancias = user_info.get("ganancias_acumuladas", 0) >= monto_solicitado
            comision = monto_solicitado * (COMISION_RETIRO_GANANCIAS if tiene_ganancias else COMISION_RETIRO_DEPOSITO)
            
            if tiene_ganancias: user_info["ganancias_acumuladas"] -= monto_solicitado
            else: user_info["deposito"] -= monto_solicitado
            
            del data["estados_retiro"][user_id]
            guardar_data(data)
            await update.message.reply_text("✅ Solicitud enviada al administrador.", reply_markup=obtener_menu(is_reg))
            await context.bot.send_message(ADMIN_TELEGRAM_ID, f"🚨 *NUEVA SOLICITUD*\n👤 {user_info['nombre']}\n💰 {monto_solicitado:.2f} USDT\n🔗 `{wallet}`", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Retiro Enviado", callback_data=f"ret_{user_id}")]]))
            return

    await update.message.reply_text("Usa el menú:", reply_markup=obtener_menu(is_reg))

async def boton_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data_cb = query.data
    user_id = str(query.from_user.id)
    data = obtener_data()
    
    if data_cb.startswith("act_"):
        target_id = data_cb.split("_")[1]
        data["usuarios"][target_id]["activo"] = True
        if target_id in data.get("pendientes_referido", {}):
            ref_id = data["pendientes_referido"][target_id]
            comision = data["usuarios"][target_id].get("deposito", 0) * COMISION_REFERIDO
            data["usuarios"][ref_id]["ganancias_acumuladas"] += comision
            del data["pendientes_referido"][target_id]
        guardar_data(data)
        await query.edit_message_text("✅ Usuario activado.")
        await context.bot.send_message(int(target_id), "🎉 ¡Cuenta ACTIVADA!", reply_markup=obtener_menu(True))
        return

    if data_cb == "pedir_retiro":
        if not data["usuarios"][user_id].get("activo"):
            await query.message.reply_text("⚠️ Cuenta inactiva.")
            return
        if datetime.now().weekday() not in [3, 4]:
            await query.message.reply_text("⚠️ Retiros solo jueves y viernes.")
            return
        data["estados_retiro"][user_id] = {"fase": "monto"}
        guardar_data(data)
        await query.message.reply_text("Escribe el monto a retirar:")
        return

    # ... (Resto de funciones de callback sin cambios para brevedad) ...
    if data_cb == "iniciar_registro":
        data["estados_registro"][user_id] = {"paso": 1}
        guardar_data(data)
        await query.message.reply_text("Nombre completo:")
    elif data_cb == "ver_saldo":
        u = data["usuarios"][user_id]
        await query.message.reply_text(f"💵 Paquete: {u['deposito']} USDT\n📈 Ganancias: {u['ganancias_acumuladas']:.2f} USDT", parse_mode="Markdown")

async def main():
    await start_web_server()
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CallbackQueryHandler(boton_callback))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, manejar_mensajes_texto))
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
