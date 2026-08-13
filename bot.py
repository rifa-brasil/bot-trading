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

COMISION_RETIRO_GANANCIAS = 0.01  # 1%
COMISION_RETIRO_DEPOSITO = 0.02   # 2%
COMISION_REFERIDO = 0.02          # 2%

PAQUETES_DISPONIBLES = [100, 120, 150, 180, 200, 250, 300, 350, 500, 1000, 1500, 2000]

def obtener_data():
    if not os.path.exists(DB_FILE):
        return {"usuarios": {}, "estados_registro": {}, "estados_retiro": {}, "estados_admin_retiro": {}, "pendientes_referido": {}}
    data = json.load(open(DB_FILE, "r"))
    # Asegurar que existan las llaves necesarias para evitar errores
    if "estados_admin_retiro" not in data: data["estados_admin_retiro"] = {}
    if "pendientes_referido" not in data: data["pendientes_referido"] = {}
    if "usuarios" not in data: data["usuarios"] = {}
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
    for monto in PAQUETES_DISPONIBLES:
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
    await update.message.reply_text("👋 ¡Bienvenido a la plataforma de inversión!", reply_markup=obtener_menu(is_reg))

async def manejar_mensajes_texto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    user_id = str(update.effective_user.id)
    data = obtener_data()
    texto = update.message.text.strip() if update.message.text else ""
    is_reg = user_id in data["usuarios"]

    # 1. GESTIÓN ENVÍO COMPROBANTE ADMIN
    if user_id == str(ADMIN_TELEGRAM_ID) and user_id in data.get("estados_admin_retiro", {}):
        target_id = data["estados_admin_retiro"][user_id]
        if update.message.photo:
            photo_file_id = update.message.photo[-1].file_id
            del data["estados_admin_retiro"][user_id]
            if target_id in data["usuarios"]: data["usuarios"][target_id]["ultimo_retiro_fecha"] = datetime.now().strftime("%Y-%m-%d")
            guardar_data(data)
            await update.message.reply_text("✅ Comprobante enviado al usuario.")
            try:
                await context.bot.send_photo(int(target_id), photo_file_id, caption="🎉 ¡Tu solicitud de retiro ha sido procesada con éxito! Adjuntamos el comprobante.", reply_markup=obtener_menu(True))
            except: pass
            return
        else:
            await update.message.reply_text("⚠️ Adjunta la **imagen** del comprobante.")
            return

    # 2. REGISTRO
    if user_id in data.get("estados_registro", {}):
        estado_reg = data["estados_registro"][user_id]
        if estado_reg["paso"] == 1:
            estado_reg.update({"nombre": texto, "paso": 2})
            guardar_data(data)
            await update.message.reply_text("📧 Ingresa tu correo:")
        elif estado_reg["paso"] == 2:
            estado_reg.update({"email": texto, "paso": 3})
            guardar_data(data)
            await update.message.reply_text("📱 Ingresa tu teléfono:")
        elif estado_reg["paso"] == 3:
            estado_reg["telefono"] = texto
            guardar_data(data)
            await update.message.reply_text("💎 Selecciona el paquete:", reply_markup=obtener_teclado_paquetes())
        return

    # 3. RETIRO
    if user_id in data.get("estados_retiro", {}):
        estado_ret = data["estados_retiro"][user_id]
        if estado_ret["fase"] == "monto":
            try:
                monto_solicitado = float(texto)
                user_info = data["usuarios"][user_id]
                ganancias_disp = user_info.get("ganancias_acumuladas", 0)
                deposito_total = user_info.get("deposito", 0)
                
                if ganancias_disp > 0 and monto_solicitado < MIN_RETIRO:
                    await update.message.reply_text(f"⚠️ Mínimo para ganancias: {MIN_RETIRO} USDT.")
                    return
                
                max_disponible = ganancias_disp if ganancias_disp > 0 else deposito_total
                if monto_solicitado > max_disponible:
                    await update.message.reply_text(f"⚠️ Saldo insuficiente.")
                    return

                tiene_ganancias = ganancias_disp >= monto_solicitado
                tasa = COMISION_RETIRO_GANANCIAS if tiene_ganancias else COMISION_RETIRO_DEPOSITO
                comision = monto_solicitado * tasa
                monto_neto = monto_solicitado - comision
                
                estado_ret.update({"monto": monto_solicitado, "fase": "wallet"})
                guardar_data(data)
                await update.message.reply_text(f"📝 *Resumen:*\nSolicitado: {monto_solicitado:.2f} USDT\nComisión ({int(tasa*100)}%): {comision:.2f} USDT\nNeto a recibir: {monto_neto:.2f} USDT\n\nEscribe tu **wallet TRC20**:", parse_mode="Markdown")
            except: await update.message.reply_text("⚠️ Monto inválido.")
            return

        elif estado_ret["fase"] == "wallet":
            wallet = texto
            monto_solicitado = estado_ret["monto"]
            user_info = data["usuarios"][user_id]
            tiene_ganancias = user_info.get("ganancias_acumuladas", 0) >= monto_solicitado
            
            if tiene_ganancias: user_info["ganancias_acumuladas"] -= monto_solicitado
            else: user_info["deposito"] -= monto_solicitado
            
            del data["estados_retiro"][user_id]
            guardar_data(data)
            await update.message.reply_text("✅ Solicitud enviada.", reply_markup=obtener_menu(is_reg))
            msg_admin = f"🚨 *NUEVA SOLICITUD*\n👤 {user_info.get('nombre')}\n💰 {monto_solicitado:.2f} USDT\n🔗 `{wallet}`"
            await context.bot.send_message(ADMIN_TELEGRAM_ID, msg_admin, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Retiro Enviado", callback_data=f"ret_{user_id}")]]))
            return

    await update.message.reply_text("Usa el menú:", reply_markup=obtener_menu(is_reg))

async def boton_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data_cb = query.data
    user_id = str(query.from_user.id)
    data = obtener_data()
    
    # Manejo seguro de usuario no registrado
    is_reg = user_id in data["usuarios"]

    if data_cb.startswith("act_"):
        target_id = data_cb.split("_")[1]
        if target_id in data["usuarios"]:
            data["usuarios"][target_id]["activo"] = True
            if target_id in data.get("pendientes_referido", {}):
                ref_id = data["pendientes_referido"][target_id]
                comision = data["usuarios"][target_id].get("deposito", 0) * COMISION_REFERIDO
                data["usuarios"][ref_id]["ganancias_acumuladas"] += comision
                del data["pendientes_referido"][target_id]
            guardar_data(data)
            await query.edit_message_text("✅ Usuario activado.")
        return

    if data_cb.startswith("paq_"):
        monto = float(data_cb.split("_")[1])
        if user_id in data.get("estados_registro", {}):
            reg = data["estados_registro"].pop(user_id)
            data["usuarios"][user_id] = {"nombre": reg["nombre"], "email": reg["email"], "telefono": reg["telefono"], "deposito": monto, "ganancias_acumuladas": 0.0, "total_generado": 0.0, "ultimo_retiro_fecha": "", "activo": False}
            guardar_data(data)
            await query.message.edit_text(f"✅ Paquete de {monto} USDT seleccionado. Envía comprobante al admin.")
            await context.bot.send_message(ADMIN_TELEGRAM_ID, f"🚨 *NUEVO REGISTRO*\n👤 {reg['nombre']}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Activar", callback_data=f"act_{user_id}")]]), parse_mode="Markdown")
        return

    if data_cb == "ret_": # Corregido para capturar el ID
        pass
    
    if data_cb.startswith("ret_"):
        target_id = data_cb.split("_")[1]
        data["estados_admin_retiro"][user_id] = target_id
        guardar_data(data)
        await query.message.reply_text("📸 Envía ahora la captura del comprobante:")
        return

    if data_cb == "iniciar_registro":
        data["estados_registro"][user_id] = {"paso": 1}
        guardar_data(data)
        await query.message.reply_text("📝 Nombre completo:")
    
    elif data_cb == "ver_saldo":
        if not is_reg: return
        u = data["usuarios"][user_id]
        msg = f"📊 *SALDO*\n💵 Paquete: {u['deposito']} USDT\n📈 Ganancias: {u['ganancias_acumuladas']:.2f} USDT"
        await query.message.reply_text(msg, parse_mode="Markdown")

    elif data_cb == "ver_invitacion":
        if is_reg:
            link = f"https://t.me/{context.bot.username}?start={user_id}"
            await query.message.reply_text(f"👥 *Tu enlace de invitado:*\n`{link}`", parse_mode="Markdown")

    elif data_cb == "pedir_retiro":
        if is_reg and data["usuarios"][user_id].get("activo"):
            data["estados_retiro"][user_id] = {"fase": "monto"}
            guardar_data(data)
            await query.message.reply_text("📤 Escribe el monto:")

    elif data_cb == "ver_info":
        await query.message.reply_text("ℹ️ *Reglas:* Rendimiento 0.50% diario. Retiros Jueves/Viernes.", parse_mode="Markdown")

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
