import os
import json
import asyncio
from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, CallbackQueryHandler, filters

# --- SERVIDOR WEB PARA CUMPLIR CON EL PUERTO DE RENDER ---
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
    print(f"🌐 Servidor web corriendo en el puerto {port}")

# --- CONFIGURACIÓN ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
ADMIN_TELEGRAM_ID = int(os.environ.get("ADMIN_TELEGRAM_ID", "0"))
WALLET_EMPRESA = "TXXsV2612pJjoz6KrpfsPUqwGQryjGpRbE" 
ADMIN_USERNAME = "@yordanisr"

DB_FILE = "inversion_db.json"
PORCENTAJE_DIARIO = 0.50
MIN_INVERSION = 100.0
MIN_RETIRO = 50.0
COMISION_RETIRO = 0.01 # 1%

def inicializar_bd():
    try:
        if not os.path.exists(DB_FILE):
            data_inicial = {
                "usuarios": {},
                "estados_registro": {},
                "estados_retiro": {}
            }
            with open(DB_FILE, "w") as f:
                json.dump(data_inicial, f, indent=4)
    except Exception as e:
        print(f"Error al inicializar BD: {e}")

def obtener_data():
    inicializar_bd()
    try:
        with open(DB_FILE, "r") as f:
            return json.load(f)
    except Exception:
        inicializar_bd()
        with open(DB_FILE, "r") as f:
            return json.load(f)

def guardar_data(data):
    try:
        with open(DB_FILE, "w") as f:
            json.dump(data, f, indent=4)
    except Exception as e:
        print(f"Error al guardar BD: {e}")

# Tarea en segundo plano para sumar ganancias diarias automáticamente cada 24 horas
async def tarea_ganancias_diarias():
    while True:
        await asyncio.sleep(86400) # Espera 24 horas
        data = obtener_data()
        usuarios = data.get("usuarios", {})
        
        for uid, info in usuarios.items():
            if info.get("activo", False) and info.get("deposito", 0) >= MIN_INVERSION:
                ganancia_hoy = info["deposito"] * (PORCENTAJE_DIARIO / 100)
                info["ganancias_acumuladas"] = info.get("ganancias_acumuladas", 0) + ganancia_hoy
                
        guardar_data(data)
        print("📈 Ganancias diarias calculadas y sumadas a los inversores activos.")

def menu_principal_markup():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 Mi Saldo y Estadísticas", callback_data="ver_saldo")],
        [InlineKeyboardButton("📤 Solicitar Retiro", callback_data="pedir_retiro")],
        [InlineKeyboardButton("ℹ️ Información / Reglas", callback_data="ver_info")]
    ])

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    data = obtener_data()
    usuarios = data.get("usuarios", {})

    if user_id in usuarios:
        estado_txt = "✅ Cuenta Activa" if usuarios[user_id].get("activo") else "⏳ Cuenta Pendiente de Aprobación por el Administrador"
        await update.message.reply_text(
            f"👋 ¡Bienvenido de nuevo a tu panel de inversión!\nEstado: {estado_txt}",
            reply_markup=menu_principal_markup()
        )
    else:
        data["estados_registro"][user_id] = {"paso": 1}
        guardar_data(data)
        await update.message.reply_text(
            "👋 *¡Bienvenido a la plataforma de inversión!* 🚀\n\n"
            "Para comenzar tu registro, por favor escribe tu *Nombre completo*:",
            parse_mode="Markdown"
        )

async def manejar_mensajes_texto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    texto = update.message.text.strip()
    user_id = str(update.effective_user.id)
    data = obtener_data()
    
    # 1. GESTIÓN DEL FLUJO DE REGISTRO
    if "estados_registro" in data and user_id in data["estados_registro"]:
        paso = data["estados_registro"][user_id]["paso"]

        if paso == 1:
            data["estados_registro"][user_id]["nombre"] = texto
            data["estados_registro"][user_id]["paso"] = 2
            guardar_data(data)
            await update.message.reply_text("📧 Ahora, por favor ingresa tu *Correo electrónico* válido:", parse_mode="Markdown")
            return

        elif paso == 2:
            data["estados_registro"][user_id]["email"] = texto
            data["estados_registro"][user_id]["paso"] = 3
            guardar_data(data)
            await update.message.reply_text("📱 Ingresa tu número de *Teléfono* de contacto:", parse_mode="Markdown")
            return

        elif paso == 3:
            data["estados_registro"][user_id]["telefono"] = texto
            data["estados_registro"][user_id]["paso"] = 4
            guardar_data(data)
            await update.message.reply_text(f"💵 Ingresa el monto de tu *Depósito inicial* (Mínimo {MIN_INVERSION} USDT):", parse_mode="Markdown")
            return

        elif paso == 4:
            try:
                deposito = float(texto)
                if deposito < MIN_INVERSION:
                    await update.message.reply_text(f"⚠️ El monto mínimo de inversión es de *{MIN_INVERSION} USDT*.", parse_mode="Markdown")
                    return
                
                reg_info = data["estados_registro"][user_id]
                nombre = reg_info["nombre"]
                email = reg_info["email"]
                telefono = reg_info["telefono"]

                data["usuarios"][user_id] = {
                    "nombre": nombre,
                    "email": email,
                    "telefono": telefono,
                    "deposito": deposito,
                    "ganancias_acumuladas": 0.0,
                    "wallet": "",
                    "activo": False 
                }
                
                del data["estados_registro"][user_id]
                guardar_data(data)

                # Mensaje para el usuario incluyendo tu username @yordanisr
                mensaje_final = (
                    f"✅ *¡Registro y depósito registrado en el sistema!* 🎉\n\n"
                    f"• Inversión declarada: *{deposito} USDT*\n\n"
                    f"Para activar tu cuenta, realiza la transferencia a nuestra billetera oficial TRC20:\n\n"
                    f"`{WALLET_EMPRESA}`\n\n"
                    f"⚠️ *Importante:* Tu cuenta se encuentra en revisión. Por favor, **envía el comprobante de pago al privado del administrador {ADMIN_USERNAME}** para que verifique y active tu cuenta manualmente."
                )
                await update.message.reply_text(mensaje_final, reply_markup=menu_principal_markup(), parse_mode="Markdown")
                
                # Envía mensaje directo al administrador notificando el registro pendiente
                if ADMIN_TELEGRAM_ID:
                    try:
                        await context.bot.send_message(
                            chat_id=ADMIN_TELEGRAM_ID,
                            text=f"🚨 *NUEVO REGISTRO / DEPÓSITO PENDIENTE*\n\n"
                                 f"👤 Nombre: {nombre}\n"
                                 f"🆔 ID: `{user_id}`\n"
                                 f"📧 Email: {email}\n"
                                 f"📱 Teléfono: {telefono}\n"
                                 f"💵 Monto: ${deposito:.2f} USDT\n\n"
                                 f"👉 El usuario te enviará el comprobante al privado. Para activar su cuenta manualmente, usa el comando:\n"
                                 f"`/activar {user_id}`",
                            parse_mode="Markdown"
                        )
                    except Exception as e:
                        print(f"Error al notificar al admin: {e}")

            except ValueError:
                await update.message.reply_text("⚠️ Por favor ingresa un monto numérico válido para el depósito (ej: 100).")
            return

    # 2. GESTIÓN DE LA SOLICITUD DE RETIRO
    if "estados_retiro" in data and user_id in data["estados_retiro"]:
        wallet = texto
        data["usuarios"][user_id]["wallet"] = wallet
        
        del data["estados_retiro"][user_id]
        guardar_data(data)

        await update.message.reply_text(
            f"✅ *Dirección de Wallet guardada:* `{wallet}`\n\n"
            "📤 Tu solicitud de retiro ha sido enviada al administrador. Recuerda que los retiros son manuales.",
            reply_markup=menu_principal_markup(),
            parse_mode="Markdown"
        )
        
        user_info = data["usuarios"][user_id]
        saldo_disp = user_info["ganancias_acumuladas"]
        if ADMIN_TELEGRAM_ID:
            try:
                await context.bot.send_message(
                    chat_id=ADMIN_TELEGRAM_ID,
                    text=f"🚨 *NUEVA SOLICITUD DE RETIRO*\n\n"
                         f"👤 Usuario: {user_info['nombre']}\n"
                         f"🆔 ID: `{user_id}`\n"
                         f"💰 Ganancias a retirar: ${saldo_disp:.2f}\n"
                         f"🔗 Wallet: `{wallet}`",
                    parse_mode="Markdown"
                )
            except Exception as e:
                print(f"Error al notificar retiro al admin: {e}")
        return

    await update.message.reply_text(
        "Usa los botones del menú para interactuar con tu cuenta:",
        reply_markup=menu_principal_markup()
    )

async def activar_usuario_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_TELEGRAM_ID:
        return

    if not context.args:
        await update.message.reply_text("⚠️ Uso correcto: `/activar <ID_usuario>`", parse_mode="Markdown")
        return

    target_id = context.args[0]
    data = obtener_data()

    if target_id in data["usuarios"]:
        data["usuarios"][target_id]["activo"] = True
        guardar_data(data)
        await update.message.reply_text(f"✅ ¡Cuenta del usuario `{target_id}` activada con éxito!", parse_mode="Markdown")
        
        try:
            await context.bot.send_message(
                chat_id=int(target_id),
                text="🎉 ¡Excelentes noticias! El administrador ha verificado tu depósito y tu cuenta ya se encuentra **ACTIVA** generando rendimientos diarios.",
                reply_markup=menu_principal_markup(),
                parse_mode="Markdown"
            )
        except Exception as e:
            print(f"No se pudo notificar al usuario: {e}")
    else:
        await update.message.reply_text("❌ No se encontró ningún usuario registrado con ese ID.")

async def boton_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data_cb = query.data
    user_id = str(query.from_user.id)
    data = obtener_data()
    usuarios = data.get("usuarios", {})

    if user_id not in usuarios and data_cb != "ver_info":
        await query.message.reply_text("⚠️ Debes completar tu registro primero escribiendo /start", reply_markup=menu_principal_markup())
        return

    if data_cb == "ver_saldo":
        info = usuarios[user_id]
        deposito = info.get("deposito", 0)
        ganancias = info.get("ganancias_acumuladas", 0)
        total = deposito + ganancias
        estado_cta = "✅ Activa" if info.get("activo") else "⏳ Pendiente de aprobación"
        
        texto_saldo = (
            f"📊 *ESTADÍSTICAS Y SALDO DE TU CUENTA* 📊\n\n"
            f"📌 Estado: *{estado_cta}*\n"
            f"💵 Depósito Inicial: *${deposito:.2f} USDT*\n"
            f"📈 Ganancias Acumuladas: *${ganancias:.2f} USDT* (Calculadas al {PORCENTAJE_DIARIO}% diario)\n"
            f"💎 *Saldo Total Disponible:* *${total:.2f} USDT*\n"
        )
        await query.message.reply_text(texto_saldo, reply_markup=menu_principal_markup(), parse_mode="Markdown")

    elif data_cb == "pedir_retiro":
        info = usuarios[user_id]
        if not info.get("activo"):
            await query.message.reply_text("⚠️ Tu cuenta aún está pendiente de activación por el administrador. No puedes retirar todavía.", reply_markup=menu_principal_markup())
            return

        ganancias = info.get("ganancias_acumuladas", 0)
        if ganancias < MIN_RETIRO:
            await query.message.reply_text(
                f"⚠️ El monto mínimo para solicitar un retiro es de *{MIN_RETIRO} USDT*. Tienes acumulado: *${ganancias:.2f} USDT*.",
                reply_markup=menu_principal_markup(),
                parse_mode="Markdown"
            )
            return

        if "estados_retiro" not in data:
            data["estados_retiro"] = {}
        data["estados_retiro"][user_id] = True
        guardar_data(data)

        await query.message.reply_text(
            f"📤 *SOLICITUD DE RETIRO*\n\n"
            f"Tienes disponible: *${ganancias:.2f} USDT*\n"
            f"• Comisión por retiro: *{int(COMISION_RETIRO*100)}%*\n\n"
            "Por favor, responde a este mensaje escribiendo la *dirección de tu wallet* TRC20 donde deseas recibir el pago:",
            reply_markup=menu_principal_markup(),
            parse_mode="Markdown"
        )

    elif data_cb == "ver_info":
        texto_info = (
            "ℹ️ *INFORMACIÓN Y REGLAS DE LA PLATAFORMA* ℹ️\n\n"
            f"• *Rendimiento Diario:* Generamos un rendimiento diario del *{PORCENTAJE_DIARIO}%* sobre tu capital depositado.\n"
            f"• *Inversión Mínima:* El monto mínimo para invertir es de *{MIN_INVERSION} USDT (Red TRC20)*.\n"
            "• *Días de Retiro:* Los retiros se procesan *1 vez por semana*, exclusivamente los días *Viernes*.\n"
            f"• *Retiro Mínimo:* El saldo mínimo requerido para solicitar un retiro es de *{MIN_RETIRO} USDT*.\n"
            f"• *Comisión:* Todos los retiros tienen una comisión del *{int(COMISION_RETIRO*100)}%*.\n"
            "• *Procesamiento:* Los retiros y activaciones de cuentas se realizan de forma manual, por lo que pedimos a los inversores *paciencia*."
        )
        await query.message.reply_text(texto_info, reply_markup=menu_principal_markup(), parse_mode="Markdown")

async def main():
    inicializar_bd()
    await start_web_server()

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("activar", activar_usuario_command))
    app.add_handler(CallbackQueryHandler(boton_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, manejar_mensajes_texto))

    asyncio.create_task(tarea_ganancias_diarias())

    print("🤖 Bot de Inversión Cripto iniciado correctamente...")
    
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)

    stop_signal = asyncio.Event()
    await stop_signal.wait()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Bot detenido correctamente.")
