import os
import json
import asyncio
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
MIN_INVERSION = 100.0
MIN_RETIRO = 50.0
COMISION_RETIRO = 0.01

def obtener_data():
    if not os.path.exists(DB_FILE):
        return {"usuarios": {}, "estados_registro": {}, "estados_retiro": {}, "estados_admin_retiro": {}}
    data = json.load(open(DB_FILE, "r"))
    if "estados_admin_retiro" not in data:
        data["estados_admin_retiro"] = {}
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
        [InlineKeyboardButton("ℹ️ Información / Reglas", callback_data="ver_info")]
    ])

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    data = obtener_data()
    is_reg = user_id in data["usuarios"]
    
    await update.message.reply_text(
        "👋 ¡Bienvenido a la plataforma de inversión!" if not is_reg else "👋 ¡Bienvenido de nuevo a tu panel!",
        reply_markup=obtener_menu(is_reg)
    )

async def manejar_mensajes_texto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    user_id = str(update.effective_user.id)
    data = obtener_data()
    texto = update.message.text.strip() if update.message.text else ""
    is_reg = user_id in data["usuarios"]

    # 1. GESTIÓN DE ENVÍO DE COMPROBANTE DE RETIRO POR PARTE DEL ADMIN
    if user_id == str(ADMIN_TELEGRAM_ID) and user_id in data.get("estados_admin_retiro", {}):
        target_id = data["estados_admin_retiro"][user_id]
        
        # Verificamos si el admin mandó una foto con el comprobante
        if update.message.photo:
            photo_file_id = update.message.photo[-1].file_id
            
            # Limpiamos el estado
            del data["estados_admin_retiro"][user_id]
            guardar_data(data)
            
            # Notificamos al admin
            await update.message.reply_text("✅ Comprobante enviado con éxito al usuario.")
            
            # Enviamos la foto y notificación al usuario afectado al privado
            try:
                await context.bot.send_photo(
                    chat_id=int(target_id),
                    photo=photo_file_id,
                    caption="🎉 ¡Tu solicitud de retiro ha sido procesada con éxito! Adjuntamos el comprobante de la transferencia.",
                    reply_markup=obtener_menu(True)
                )
            except Exception as e:
                await update.message.reply_text(f"⚠️ No se pudo enviar la foto al usuario: {e}")
            return
        else:
            await update.message.reply_text("⚠️ Por favor, adjunta la **imagen/captura** del comprobante de pago.")
            return

    # 2. GESTIÓN DE REGISTRO
    if user_id in data.get("estados_registro", {}):
        paso = data["estados_registro"][user_id]["paso"]
        if paso == 1:
            data["estados_registro"][user_id].update({"nombre": texto, "paso": 2})
            await update.message.reply_text("📧 Ingresa tu correo electrónico:")
        elif paso == 2:
            data["estados_registro"][user_id].update({"email": texto, "paso": 3})
            await update.message.reply_text("📱 Ingresa tu teléfono:")
        elif paso == 3:
            data["estados_registro"][user_id].update({"telefono": texto, "paso": 4})
            await update.message.reply_text(f"💵 Monto a invertir (Mínimo {MIN_INVERSION}):")
        elif paso == 4:
            try:
                deposito = float(texto)
                if deposito < MIN_INVERSION: raise ValueError
                reg = data["estados_registro"][user_id]
                data["usuarios"][user_id] = {"nombre": reg["nombre"], "email": reg["email"], "telefono": reg["telefono"], "deposito": deposito, "ganancias_acumuladas": 0.0, "activo": False}
                del data["estados_registro"][user_id]
                guardar_data(data)
                
                await update.message.reply_text(f"✅ Registro recibido. Envía el comprobante a {ADMIN_USERNAME}.\n\nWallet: `{WALLET_EMPRESA}`", parse_mode="Markdown", reply_markup=obtener_menu(True))
                
                keyboard = [[InlineKeyboardButton("✅ Activar", callback_data=f"act_{user_id}"), InlineKeyboardButton("❌ Rechazar", callback_data=f"rej_{user_id}")]]
                await context.bot.send_message(ADMIN_TELEGRAM_ID, f"🚨 *NUEVO REGISTRO*\n👤 {reg['nombre']}\n💵 {deposito} USDT", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
            except: 
                await update.message.reply_text("⚠️ Monto no válido.")
        guardar_data(data)
        return

    # 3. GESTIÓN DE RETIRO
    if user_id in data.get("estados_retiro", {}):
        user_info = data["usuarios"][user_id]
        wallet = texto
        monto_retiro = user_info["ganancias_acumuladas"]
        
        data["usuarios"][user_id]["wallet"] = wallet
        del data["estados_retiro"][user_id]
        guardar_data(data)
        
        await update.message.reply_text("✅ Solicitud de retiro procesada y enviada al administrador.", reply_markup=obtener_menu(is_reg))
        
        keyboard_admin = [[InlineKeyboardButton("📤 Retiro Enviado (Adjuntar Comprobante)", callback_data=f"ret_{user_id}")]]
        msg_admin = (
            f"🚨 *NUEVA SOLICITUD DE RETIRO*\n\n"
            f"👤 Usuario: {user_info['nombre']}\n"
            f"🆔 ID: `{user_id}`\n"
            f"💰 Monto a retirar: {monto_retiro:.2f} USDT\n"
            f"🔗 Wallet: `{wallet}`"
        )
        await context.bot.send_message(ADMIN_TELEGRAM_ID, msg_admin, reply_markup=InlineKeyboardMarkup(keyboard_admin), parse_mode="Markdown")
        return

    await update.message.reply_text("Usa los botones para navegar:", reply_markup=obtener_menu(is_reg))

async def boton_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data_cb = query.data
    user_id = str(query.from_user.id)
    data = obtener_data()
    is_reg = user_id in data["usuarios"]

    # Acción Admin: Activar cuenta de registro
    if data_cb.startswith("act_") or data_cb.startswith("rej_"):
        target_id = data_cb.split("_")[1]
        if data_cb.startswith("act_"):
            data["usuarios"][target_id]["activo"] = True
            await context.bot.send_message(int(target_id), "🎉 ¡Cuenta ACTIVADA!", reply_markup=obtener_menu(True))
            await query.edit_message_text(f"✅ Usuario {target_id} activado.")
        else:
            del data["usuarios"][target_id]
            await query.edit_message_text(f"❌ Usuario {target_id} rechazado.")
        guardar_data(data)
        return

    # Acción Admin: Iniciar proceso de envío de comprobante de retiro
    if data_cb.startswith("ret_"):
        if user_id != str(ADMIN_TELEGRAM_ID): return
        target_id = data_cb.split("_")[1]
        
        if "estados_admin_retiro" not in data:
            data["estados_admin_retiro"] = {}
        data["estados_admin_retiro"][user_id] = target_id
        guardar_data(data)
        
        await query.message.reply_text("📸 Por favor, **envía ahora la captura de pantalla o foto del comprobante** de pago para este usuario:")
        return

    # Acciones Usuario
    if data_cb == "iniciar_registro":
        data["estados_registro"][user_id] = {"paso": 1}
        guardar_data(data)
        await query.message.reply_text("📝 Escribe tu nombre completo:")
    
    elif data_cb == "ver_saldo":
        if is_reg:
            u = data["usuarios"][user_id]
            deposito = u.get("deposito", 0)
            ganancias = u.get("ganancias_acumuladas", 0)
            total_disponible = deposito + ganancias
            estado = "✅ Activa" if u.get("activo") else "⏳ Pendiente de aprobación"
            
            mensaje_saldo = (
                f"📊 *ESTADO DE TU CUENTA*\n\n"
                f"👤 *Usuario:* {u.get('nombre')}\n"
                f"📌 *Estado:* {estado}\n\n"
                f"💵 *Depósito Inicial:* {deposito:.2f} USDT\n"
                f"📈 *Ganancias Acumuladas:* {ganancias:.2f} USDT\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"💎 *TOTAL DISPONIBLE:* {total_disponible:.2f} USDT"
            )
            await query.message.reply_text(mensaje_saldo, parse_mode="Markdown", reply_markup=obtener_menu(True))
        else:
            await query.message.reply_text("⚠️ No encontramos un registro activo para tu cuenta.", reply_markup=obtener_menu(False))

    elif data_cb == "pedir_retiro":
        if is_reg and data["usuarios"][user_id].get("activo"):
            data["estados_retiro"][user_id] = True
            guardar_data(data)
            await query.message.reply_text("📤 Por favor, escribe o pega la dirección de tu wallet TRC20 para el retiro:")
        else:
            await query.message.reply_text("⚠️ Tu cuenta debe estar activa para solicitar retiros.", reply_markup=obtener_menu(is_reg))

    elif data_cb == "ver_info":
        texto_info = (
            "ℹ️ *INFORMACIÓN Y REGLAS DE LA PLATAFORMA* ℹ️\n\n"
            f"• *Rendimiento:* Generamos un {PORCENTAJE_DIARIO}% diario sobre tu capital depositado.\n"
            f"• *Inversión Mínima:* {MIN_INVERSION} USDT (Red TRC20).\n\n"
            "🗓 *CRONOGRAMA DE OPERACIONES:*\n"
            "• *Activación:* Las cuentas se activan manualmente tras verificar el comprobante de pago enviado al administrador.\n"
            "• *Retiros:* Se procesan exclusivamente los días **viernes** de cada semana.\n"
            f"• *Mínimo de Retiro:* {MIN_RETIRO} USDT.\n"
            f"• *Comisión de Retiro:* {int(COMISION_RETIRO * 100)}% por transacción.\n\n"
            "⚠️ *NOTAS IMPORTANTES:*\n"
            "1. Asegúrate de enviar el comprobante de depósito al privado del administrador para procesar tu activación.\n"
            "2. Toda inversión conlleva riesgos; esta plataforma opera bajo un modelo de gestión manual.\n"
            "3. Mantén siempre tu enlace con el bot actualizado para recibir notificaciones sobre tus retiros y estados de cuenta.\n\n"
            f"¿Tienes dudas adicionales? Contacta a soporte a través de {ADMIN_USERNAME}"
        )
        await query.message.reply_text(texto_info, parse_mode="Markdown", reply_markup=obtener_menu(is_reg))

async def main():
    await start_web_server()
    
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CallbackQueryHandler(boton_callback))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, manejar_mensajes_texto))

    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    print("🤖 Bot activo...")

    stop_signal = asyncio.Event()
    try:
        await stop_signal.wait()
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Bot detenido correctamente.")
