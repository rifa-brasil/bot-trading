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
COMISION_RETIRO = 0.01

# Paquetes de inversión disponibles
PAQUETES_DISPONIBLES = [100, 120, 150, 180, 200, 250, 300, 350, 500, 1000, 1500, 2000]

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

# Teclado inline con los paquetes de inversión
def obtener_teclado_paquetes():
    keyboard = []
    fila = []
    for i, monto in enumerate(PAQUETES_DISPONIBLES):
        fila.append(InlineKeyboardButton(f"💎 {monto} USDT", callback_data=f"paq_{monto}"))
        if len(fila) == 3:  # 3 botones por fila
            keyboard.append(fila)
            fila = []
    if fila:
        keyboard.append(fila)
    return InlineKeyboardMarkup(keyboard)

# Función para verificar si una fecha (string YYYY-MM-DD) pertenece a la semana en curso (lunes a domingo)
def es_misma_semana(fecha_str):
    if not fecha_str:
        return False
    try:
        fecha_retiro = datetime.strptime(fecha_str, "%Y-%m-%d").date()
        hoy = datetime.now().date()
        lunes_hoy = hoy - timedelta(days=hoy.weekday())
        lunes_retiro = fecha_retiro - timedelta(days=fecha_retiro.weekday())
        return lunes_hoy == lunes_retiro
    except:
        return False

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
        
        if update.message.photo:
            photo_file_id = update.message.photo[-1].file_id
            del data["estados_admin_retiro"][user_id]
            
            if target_id in data["usuarios"]:
                data["usuarios"][target_id]["ultimo_retiro_fecha"] = datetime.now().strftime("%Y-%m-%d")
            
            guardar_data(data)
            
            await update.message.reply_text("✅ Comprobante enviado con éxito al usuario y registrado el retiro de la semana.")
            
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

    # 2. GESTIÓN DE REGISTRO (Paso 1, 2 y 3: Nombre, Email, Teléfono)
    if user_id in data.get("estados_registro", {}):
        estado_reg = data["estados_registro"][user_id]
        paso = estado_reg["paso"]

        if paso == 1:
            estado_reg["nombre"] = texto
            estado_reg["paso"] = 2
            guardar_data(data)
            await update.message.reply_text("📧 Ingresa tu correo electrónico:")
            return

        elif paso == 2:
            estado_reg["email"] = texto
            estado_reg["paso"] = 3
            guardar_data(data)
            await update.message.reply_text("📱 Ingresa tu teléfono:")
            return

        elif paso == 3:
            estado_reg["telefono"] = texto
            guardar_data(data)
            await update.message.reply_text(
                "💎 Selecciona el paquete de inversión que deseas adquirir utilizando los botones de abajo:",
                reply_markup=obtener_teclado_paquetes()
            )
            return

    # 3. GESTIÓN DE RETIRO (Monto que desea retirar)
    if user_id in data.get("estados_retiro", {}):
        estado_ret = data["estados_retiro"][user_id]
        fase = estado_ret["fase"]

        if fase == "monto":
            try:
                monto_solicitado = float(texto)
                user_info = data["usuarios"][user_id]
                ganancias_disp = user_info.get("ganancias_acumuladas", 0)

                # Validación de mínimo de retiro solo si tiene ganancias generadas
                if ganancias_disp > 0 and monto_solicitado < MIN_RETIRO:
                    await update.message.reply_text(f"⚠️ El monto mínimo de retiro de ganancias es de {MIN_RETIRO} USDT.")
                    return
                
                # Validar que no retire más de lo disponible (ganancias o depósito si no hay ganancias)
                max_disponible = ganancias_disp if ganancias_disp > 0 else user_info.get("deposito", 0)
                if monto_solicitado > max_disponible:
                    await update.message.reply_text(f"⚠️ No tienes suficiente saldo disponible. Tu saldo máximo disponible es: {max_disponible:.2f} USDT.")
                    return

                estado_ret["monto"] = monto_solicitado
                estado_ret["fase"] = "wallet"
                guardar_data(data)

                await update.message.reply_text("📤 Ahora, escribe o pega la dirección de tu **wallet TRC20** donde deseas recibir el pago:")
                return

            except ValueError:
                await update.message.reply_text("⚠️ Por favor ingresa un monto numérico válido.")
                return

        elif fase == "wallet":
            wallet = texto
            monto_solicitado = estado_ret["monto"]
            user_info = data["usuarios"][user_id]

            # Descontar de ganancias o de depósito según corresponda
            if user_info.get("ganancias_acumuladas", 0) > 0:
                user_info["ganancias_acumuladas"] -= monto_solicitado
            else:
                user_info["deposito"] -= monto_solicitado
                if user_info["deposito"] <= 0:
                    user_info["activo"] = False

            user_info["wallet"] = wallet
            
            del data["estados_retiro"][user_id]
            guardar_data(data)

            await update.message.reply_text(f"✅ Solicitud de retiro por {monto_solicitado:.2f} USDT procesada y enviada al administrador.", reply_markup=obtener_menu(is_reg))

            keyboard_admin = [[InlineKeyboardButton("📤 Retiro Enviado (Adjuntar Comprobante)", callback_data=f"ret_{user_id}")]]
            msg_admin = (
                f"🚨 *NUEVA SOLICITUD DE RETIRO*\n\n"
                f"👤 Usuario: {user_info['nombre']}\n"
                f"🆔 ID: `{user_id}`\n"
                f"💰 Monto solicitado: {monto_solicitado:.2f} USDT\n"
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

    # Selección de Paquete de Inversión (Durante Registro)
    if data_cb.startswith("paq_"):
        monto_paquete = float(data_cb.split("_")[1])
        if user_id in data.get("estados_registro", {}):
            reg = data["estados_registro"][user_id]
            data["usuarios"][user_id] = {
                "nombre": reg["nombre"],
                "email": reg["email"],
                "telefono": reg["telefono"],
                "deposito": monto_paquete,
                "ganancias_acumuladas": 0.0,
                "total_generado": 0.0,
                "ultimo_retiro_fecha": "",
                "activo": False
            }
            del data["estados_registro"][user_id]
            guardar_data(data)

            await query.message.edit_text(
                f"✅ *Paquete seleccionado:* {monto_paquete} USDT\n\n"
                f"Para activar tu cuenta, realiza la transferencia a nuestra billetera oficial TRC20:\n\n"
                f"`{WALLET_EMPRESA}`\n\n"
                f"⚠️ Envía el comprobante de pago al administrador {ADMIN_USERNAME}.",
                parse_mode="Markdown"
            )
            await query.message.reply_text("Panel principal:", reply_markup=obtener_menu(True))

            keyboard = [[InlineKeyboardButton("✅ Activar", callback_data=f"act_{user_id}"), InlineKeyboardButton("❌ Rechazar", callback_data=f"rej_{user_id}")]]
            await context.bot.send_message(ADMIN_TELEGRAM_ID, f"🚨 *NUEVO REGISTRO*\n👤 {reg['nombre']}\n💵 Paquete: {monto_paquete} USDT", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
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
            total_generado = u.get("total_generado", 0)
            meta_200 = deposito * 2.0
            
            porcentaje_progreso = (total_generado / meta_200) * 100 if meta_200 > 0 else 0
            if porcentaje_progreso > 200: porcentaje_progreso = 200.0

            estado = "✅ Activa" if u.get("activo") else "⏳ Pendiente de aprobación"
            
            mensaje_saldo = (
                f"📊 *ESTADO DE TU CUENTA*\n\n"
                f"👤 *Usuario:* {u.get('nombre')}\n"
                f"📌 *Estado:* {estado}\n\n"
                f"💵 *Paquete Activo:* {deposito:.2f} USDT\n"
                f"📈 *Ganancias Disponibles:* {ganancias:.2f} USDT\n"
                f"🎯 *Progreso hacia el 200%:* {porcentaje_progreso:.1f}% "
                f"({total_generado:.2f} / {meta_200:.2f} USDT)\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"💎 *SALDO DISPONIBLE:* {ganancias if ganancias > 0 else deposito:.2f} USDT"
            )
            await query.message.reply_text(mensaje_saldo, parse_mode="Markdown", reply_markup=obtener_menu(True))
        else:
            await query.message.reply_text("⚠️ No encontramos un registro activo para tu cuenta.", reply_markup=obtener_menu(False))

    elif data_cb == "pedir_retiro":
        if is_reg and data["usuarios"][user_id].get("activo"):
            u_info = data["usuarios"][user_id]
            
            # Validación de día (Jueves 3, Viernes 4)
            dia_actual = datetime.now().weekday()
            if dia_actual not in [3, 4]:
                await query.message.reply_text("⚠️ Los retiros solo están habilitados los días **jueves y viernes**.", reply_markup=obtener_menu(is_reg))
                return

            # Validar retiro semanal
            ultimo_retiro = u_info.get("ultimo_retiro_fecha", "")
            if es_misma_semana(ultimo_retiro):
                await query.message.reply_text("⚠️ Ya realizaste el retiro correspondiente a esta semana.", reply_markup=obtener_menu(is_reg))
                return

            ganancias_disp = u_info.get("ganancias_acumuladas", 0)
            deposito_inicial = u_info.get("deposito", 0)

            se_puede_retirar = False
            if ganancias_disp > 0:
                if ganancias_disp >= MIN_RETIRO:
                    se_puede_retirar = True
                    mensaje_retiro = f"📤 Tienes {ganancias_disp:.2f} USDT disponibles de ganancias."
                else:
                    await query.message.reply_text(f"⚠️ Para retirar ganancias, el mínimo es {MIN_RETIRO} USDT.", reply_markup=obtener_menu(is_reg))
                    return
            else:
                se_puede_retirar = True
                mensaje_retiro = f"📤 No tienes ganancias generadas. Puedes retirar tu capital inicial ({deposito_inicial:.2f} USDT) sin mínimo."

            if se_puede_retirar:
                if "estados_retiro" not in data:
                    data["estados_retiro"] = {}
                data["estados_retiro"][user_id] = {"fase": "monto"}
                guardar_data(data)
                await query.message.reply_text(f"{mensaje_retiro}\n\nPor favor, escribe la cantidad que deseas retirar:", parse_mode="Markdown")
        else:
            await query.message.reply_text("⚠️ Tu cuenta debe estar activa para solicitar retiros.", reply_markup=obtener_menu(is_reg))

    elif data_cb == "ver_info":
        texto_info = (
            "ℹ️ *INFORMACIÓN Y REGLAS DE LA PLATAFORMA* ℹ️\n\n"
            f"• *Rendimiento:* Generamos un {PORCENTAJE_DIARIO}% diario sobre tu capital depositado.\n"
            f"• *Límite del Paquete:* Cada paquete de inversión tiene validez hasta alcanzar el *200% de retorno* sobre la inversión inicial.\n\n"
            "🗓 *CRONOGRAMA DE OPERACIONES:*\n"
            "• *Activación:* Las cuentas se activan manualmente tras verificar el comprobante de pago enviado al administrador.\n"
            "• *Retiros:* Se procesan exclusivamente los días **jueves y viernes** de cada semana (1 retiro por semana por usuario).\n"
            f"• *Mínimo de Retiro:* {MIN_RETIRO} USDT (solo aplica para ganancias).\n"
            f"• *Comisión de Retiro:* {int(COMISION_RETIRO * 100)}% por transacción.\n\n"
            "⚠️ *NOTAS IMPORTANTES:*\n"
            "1. Asegúrate de enviar el comprobante de depósito al privado del administrador para procesar tu activación.\n"
            "2. Si retiras tu capital sin generar ganancias, puedes hacerlo sin mínimo de retiro.\n"
            f"¿Tienes dudas adicionales? Contacta a soporte a través de {ADMIN_USERNAME}"
        )
        await query.message.reply_text(texto_info, parse_mode="Markdown", reply_markup=obtener_menu(is_reg))

# Tarea en segundo plano para sumar ganancias diarias y controlar el límite del 200%
async def tarea_ganancias_diarias():
    while True:
        await asyncio.sleep(86400) # Cada 24 horas
        data = obtener_data()
        usuarios = data.get("usuarios", {})
        
        for uid, info in usuarios.items():
            if info.get("activo", False):
                deposito = info.get("deposito", 0)
                meta_200 = deposito * 2.0
                total_generado = info.get("total_generado", 0.0)

                if total_generado >= meta_200:
                    continue

                ganancia_hoy = deposito * (PORCENTAJE_DIARIO / 100)
                
                if total_generado + ganancia_hoy > meta_200:
                    ganancia_hoy = meta_200 - total_generado

                info["ganancias_acumuladas"] = info.get("ganancias_acumuladas", 0) + ganancia_hoy
                info["total_generado"] = total_generado + ganancia_hoy
                
        guardar_data(data)
        print("📈 Ganancias diarias calculadas y progreso del 200% actualizado.")

async def main():
    await start_web_server()
    
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CallbackQueryHandler(boton_callback))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, manejar_mensajes_texto))

    asyncio.create_task(tarea_ganancias_diarias())

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
