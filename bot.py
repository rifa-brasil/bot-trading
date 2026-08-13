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
# ---------------------------------------------------------

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
ADMIN_TELEGRAM_ID = int(os.environ.get("ADMIN_TELEGRAM_ID", "0"))

DB_FILE = "inversion_db.json"
PORCENTAJE_DIARIO = 1.5  # 1.5% de ganancia diaria por defecto

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
async def tarea_ganancias_diarias(application):
    while True:
        await asyncio.sleep(86400) # Espera 24 horas
        data = obtener_data()
        usuarios = data.get("usuarios", {})
        
        for uid, info in usuarios.items():
            deposito = info.get("deposito", 0)
            if deposito > 0:
                ganancia_hoy = deposito * (PORCENTAJE_DIARIO / 100)
                info["ganancias_acumuladas"] = info.get("ganancias_acumuladas", 0) + ganancia_hoy
                
        guardar_data(data)
        print("📈 Ganancias diarias calculadas y sumadas a los inversores.")

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
        await update.message.reply_text(
            "👋 ¡Bienvenido de nuevo a tu panel de inversión!",
            reply_markup=menu_principal_markup()
        )
    else:
        # Inicia el proceso de registro obligatorio paso 1
        data["estados_registro"][user_id] = {"paso": 1}
        guardar_data(data)
        await update.message.reply_text(
            "👋 *¡Bienvenido a la plataforma de inversión cripto!* 🚀\n\n"
            "Para comenzar tu registro obligatorio, por favor escribe tu *Nombre completo*:",
            parse_mode="Markdown"
        )

async def manejar_mensajes_texto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    texto = update.message.text.strip()
    user_id = str(update.effective_user.id)
    data = obtener_data()
    
    # 1. GESTIÓN DEL FLUJO DE REGISTRO OBLIGATORIO
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
            await update.message.reply_text("💵 Ingresa el monto de tu *Depósito inicial* (ejemplo: 100 o 500):", parse_mode="Markdown")
            return

        elif paso == 4:
            try:
                deposito = float(texto)
                if deposito <= 0:
                    raise ValueError()
                
                reg_info = data["estados_registro"][user_id]
                nombre = reg_info["nombre"]
                email = reg_info["email"]
                telefono = reg_info["telefono"]

                # Guardar usuario definitivo en la base de datos local
                data["usuarios"][user_id] = {
                    "nombre": nombre,
                    "email": email,
                    "telefono": telefono,
                    "deposito": deposito,
                    "ganancias_acumuladas": 0.0,
                    "wallet": ""
                }
                
                # Limpiar estado de registro
                del data["estados_registro"][user_id]
                guardar_data(data)

                await update.message.reply_text(
                    "✅ *¡Registro completado con éxito!* 🎉\n\n"
                    f"Tus datos han sido guardados en el sistema. Tu depósito inicial es de *${deposito}*.\n"
                    "Ya puedes gestionar tu cuenta desde el menú principal:",
                    reply_markup=menu_principal_markup(),
                    parse_mode="Markdown"
                )
            except ValueError:
                await update.message.reply_text("⚠️ Por favor ingresa un monto numérico válido para el depósito (ej: 150).")
            return

    # 2. GESTIÓN DE LA SOLICITUD DE RETIRO (INGRESO DE WALLET)
    if "estados_retiro" in data and user_id in data["estados_retiro"]:
        wallet = texto
        data["usuarios"][user_id]["wallet"] = wallet
        
        # Limpiar estado de retiro
        del data["estados_retiro"][user_id]
        guardar_data(data)

        await update.message.reply_text(
            f"✅ *Dirección de Wallet guardada:* `{wallet}`\n\n"
            "📤 Tu solicitud de retiro ha sido procesada y enviada al administrador. Te llegará a la brevedad.",
            reply_markup=menu_principal_markup(),
            parse_mode="Markdown"
        )
        
        # Notificar al administrador por Telegram
        user_info = data["usuarios"][user_id]
        saldo_disp = user_info["ganancias_acumuladas"]
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

    # Mensaje por defecto
    await update.message.reply_text(
        "Usa los botones del menú para interactuar con tu cuenta:",
        reply_markup=menu_principal_markup()
    )

async def boton_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data_cb = query.data
    user_id = str(query.from_user.id)
    data = obtener_data()
    usuarios = data.get("usuarios", {})

    if user_id not in usuarios and data_cb != "ver_info":
        await query.edit_message_text("⚠️ Debes completar tu registro primero escribiendo /start")
        return

    if data_cb == "ver_saldo":
        info = usuarios[user_id]
        deposito = info.get("deposito", 0)
        ganancias = info.get("ganancias_acumuladas", 0)
        total = deposito + ganancias
        
        texto_saldo = (
            f"📊 *ESTADÍSTICAS Y SALDO DE TU CUENTA* 📊\n\n"
            f"💵 Depósito Inicial: *${deposito:.2f}*\n"
            f"📈 Ganancias Acumuladas: *${ganancias:.2f}* (Calculadas al {PORCENTAJE_DIARIO}% diario)\n"
            f"💎 *Saldo Total Disponible:* *${total:.2f}*\n"
        )
        await query.edit_message_text(texto_saldo, reply_markup=menu_principal_markup(), parse_mode="Markdown")

    elif data_cb == "pedir_retiro":
        info = usuarios[user_id]
        ganancias = info.get("ganancias_acumuladas", 0)
        
        if ganancias <= 0:
            await query.edit_message_text(
                "⚠️ No tienes saldo acumulado de ganancias disponible para retirar en este momento.",
                reply_markup=menu_principal_markup()
            )
            return

        if "estados_retiro" not in data:
            data["estados_retiro"] = {}
        data["estados_retiro"][user_id] = True
        guardar_data(data)

        await query.edit_message_text(
            f"📤 *SOLICITUD DE RETIRO*\n\n"
            f"Tienes disponible: *${ganancias:.2f}*\n\n"
            "Por favor, responde a este mensaje escribiendo la *dirección de tu wallet* donde deseas recibir el pago:",
            parse_mode="Markdown"
        )

    elif data_cb == "ver_info":
        texto_info = (
            "ℹ️ *INFORMACIÓN DE LA PLATAFORMA*\n\n"
            f"• Generamos un rendimiento diario automático del *{PORCENTAJE_DIARIO}%* sobre tu capital depositado.\n"
            "• Los retiros se procesan de forma segura a la wallet que indiques.\n"
            "• Ante cualquier duda, contacta al soporte oficial."
        )
        await query.edit_message_text(texto_info, reply_markup=menu_principal_markup(), parse_mode="Markdown")

async def main():
    inicializar_bd()
    await start_web_server()

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CallbackQueryHandler(boton_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, manejar_mensajes_texto))

    asyncio.create_task(tarea_ganancias_diarias(app))

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
