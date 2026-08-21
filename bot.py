import os
import json
import asyncio
from datetime import datetime, timedelta, time
from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
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

PAQUETES_DISPONIBLES = [100, 120, 150, 180, 200, 250, 300, 350, 500, 1000, 1500, 2000]

def obtener_data():
    if not os.path.exists(DB_FILE):
        return {"usuarios": {}, "estados_registro": {}, "estados_retiro": {}, "estados_admin_retiro": {}, "solicitudes_hoy": []}
    data = json.load(open(DB_FILE, "r"))
    data.setdefault("estados_admin_retiro", {})
    data.setdefault("solicitudes_hoy", [])
    return data

def guardar_data(data):
    with open(DB_FILE, "w") as f:
        json.dump(data, f, indent=4)

def obtener_menu_principal(registrado: bool):
    if not registrado:
        return ReplyKeyboardMarkup([
            ['📝 Registrarse', 'ℹ️ Información']
        ], resize_keyboard=True, is_persistent=True)
    return ReplyKeyboardMarkup([
        ['💰 Mi Saldo y Estadísticas', '📤 Solicitar Retiro'],
        ['💎 Planes', '👥 Invitar Amigo'],
        ['ℹ️ Información']
    ], resize_keyboard=True, is_persistent=True)

def obtener_teclado_paquetes():
    keyboard = []
    fila = []
    for monto in PAQUETES_DISPONIBLES:
        fila.append(InlineKeyboardButton(f"💎 {monto} USDT", callback_data=f"paq_{monto}"))
        if len(fila) == 3:
            keyboard.append(fila)
            fila = []
    if fila:
        keyboard.append(fila)
    return InlineKeyboardMarkup(keyboard)

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
    
    if context.args and not is_reg:
        referidor_id = context.args[0]
        if referidor_id in data["usuarios"]:
            data.setdefault("referidos", {})[user_id] = referidor_id
            guardar_data(data)

    await update.message.reply_text(
        "👋 ¡Bienvenido a la plataforma de inversión!" if not is_reg else "👋 ¡Bienvenido de nuevo a tu panel!",
        reply_markup=obtener_menu_principal(is_reg)
    )

async def manejar_mensajes_texto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    user_id = str(update.effective_user.id)
    data = obtener_data()
    texto = update.message.text.strip() if update.message.text else ""
    is_reg = user_id in data["usuarios"]

    # 1. ADMIN ENVIANDO COMPROBANTE DE RETIRO
    if user_id == str(ADMIN_TELEGRAM_ID) and user_id in data.get("estados_admin_retiro", {}):
        target_id = data["estados_admin_retiro"][user_id]
        if update.message.photo:
            photo_file_id = update.message.photo[-1].file_id
            del data["estados_admin_retiro"][user_id]
            if target_id in data["usuarios"]:
                data["usuarios"][target_id]["ultimo_retiro_fecha"] = datetime.now().strftime("%Y-%m-%d")
            guardar_data(data)
            await update.message.reply_text("✅ Comprobante enviado con éxito.")
            try:
                await context.bot.send_photo(chat_id=int(target_id), photo=photo_file_id, caption="🎉 ¡Tu solicitud de retiro ha sido procesada con éxito!")
            except Exception as e:
                await update.message.reply_text(f"⚠️ Error enviando foto: {e}")
            return
        else:
            await update.message.reply_text("⚠️ Por favor, adjunta la imagen del comprobante.")
            return

    # 2. REGISTRO
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
            await update.message.reply_text("💎 Selecciona tu paquete:", reply_markup=obtener_teclado_paquetes())
            return

    # 3. RETIROS CON HORARIO (4:00 PM - 6:00 PM) Y VALIDACIÓN DE 24 HORAS
    if user_id in data.get("estados_retiro", {}):
        estado_ret = data["estados_retiro"][user_id]
        fase = estado_ret["fase"]

        if fase == "monto":
            try:
                monto_solicitado = float(texto)
                user_info = data["usuarios"][user_id]
                
                ganancias_disp = user_info.get("ganancias_acumuladas", 0)
                deposito = user_info.get("deposito", 0)
                fecha_activacion_str = user_info.get("fecha_activacion", "")

                # Validar si han pasado menos de 24 horas desde la activación para retirar el depósito
                puede_retirar_deposito = False
                if fecha_activacion_str:
                    f_activacion = datetime.strptime(fecha_activacion_str, "%Y-%m-%d %H:%M:%S")
                    if datetime.now() - f_activacion <= timedelta(hours=24):
                        puede_retirar_deposito = True

                saldo_total_disponible = ganancias_disp + (deposito if puede_retirar_deposito else 0)

                if monto_solicitado < MIN_RETIRO:
                    await update.message.reply_text(f"⚠️ Mínimo {MIN_RETIRO} USDT.", reply_markup=obtener_menu_principal(is_reg))
                    return
                if monto_solicitado > saldo_total_disponible:
                    await update.message.reply_text(f"⚠️ Saldo insuficiente. Disponible: {saldo_total_disponible:.2f} USDT.", reply_markup=obtener_menu_principal(is_reg))
                    return

                # Definir comisión (1% ganancias, 2% depósito)
                if monto_solicitado <= ganancias_disp:
                    porcentaje_aplicado = 0.01
                elif puede_retirar_deposito:
                    porcentaje_aplicado = 0.02
                else:
                    await update.message.reply_text("⚠️ Ya han pasado más de 24 horas desde tu depósito; no puedes retirarlo, solo tus ganancias.", reply_markup=obtener_menu_principal(is_reg))
                    return

                comision = monto_solicitado * porcentaje_aplicado
                total_neto = monto_solicitado - comision

                estado_ret.update({"monto": monto_solicitado, "comision": comision, "total_neto": total_neto, "fase": "wallet"})
                guardar_data(data)

                await update.message.reply_text(
                    f"📊 *Resumen de Retiro:*\n"
                    f"• Solicitado: {monto_solicitado:.2f} USDT\n"
                    f"• Comisión ({int(porcentaje_aplicado*100)}%): -{comision:.2f} USDT\n"
                    f"• Neto: *{total_neto:.2f} USDT*\n\n"
                    f"📤 Escribe tu wallet TRC20:", parse_mode="Markdown"
                )
                return
            except ValueError:
                await update.message.reply_text("⚠️ Ingresa un número válido.", reply_markup=obtener_menu_principal(is_reg))
                return

        elif fase == "wallet":
            wallet = texto
            monto_solicitado = estado_ret["monto"]
            comision = estado_ret["comision"]
            total_neto = estado_ret["total_neto"]
            user_info = data["usuarios"][user_id]

            if monto_solicitado <= user_info.get("ganancias_acumuladas", 0):
                user_info["ganancias_acumuladas"] -= monto_solicitado
            else:
                user_info["deposito"] = 0
                user_info["activo"] = False

            user_info["wallet"] = wallet
            
            # Registrar solicitud para el reporte automático de las 6:00 PM
            data.setdefault("solicitudes_hoy", []).append({
                "usuario": user_info["nombre"],
                "id": user_id,
                "monto": monto_solicitado,
                "neto": total_neto
            })

            del data["estados_retiro"][user_id]
            guardar_data(data)

            await update.message.reply_text("✅ Solicitud procesada y registrada para el pago diario.", reply_markup=obtener_menu_principal(is_reg))
            await context.bot.send_message(ADMIN_TELEGRAM_ID, f"🚨 *NUEVO RETIRO*\n👤 {user_info['nombre']}\n💰 Neto: {total_neto:.2f} USDT\n🔗 Wallet: `{wallet}`", parse_mode="Markdown")
            return

    # 4. BOTONES DEL MENÚ
    if texto == '📝 Registrarse':
        if is_reg:
            await update.message.reply_text("⚠️ Ya estás registrado.", reply_markup=obtener_menu_principal(True))
        else:
            data["estados_registro"][user_id] = {"paso": 1}
            guardar_data(data)
            await update.message.reply_text("📝 Escribe tu nombre completo:")

    elif texto == '💰 Mi Saldo y Estadísticas':
        if is_reg:
            u = data["usuarios"][user_id]
            deposito = u.get("deposito", 0)
            ganancias = u.get("ganancias_acumuladas", 0)
            total_gen = u.get("total_generado", 0)
            meta = deposito * 2.0
            prog = (total_gen / meta) * 100 if meta > 0 else 0
            
            await update.message.reply_text(
                f"📊 *ESTADO DE TU CUENTA*\n\n"
                f"💵 *Paquete:* {deposito:.2f} USDT\n"
                f"📈 *Ganancias:* {ganancias:.2f} USDT\n"
                f"🎯 *Progreso 200%:* {prog:.1f}%\n",
                parse_mode="Markdown", reply_markup=obtener_menu_principal(True)
            )

    elif texto == '📤 Solicitar Retiro':
        if is_reg and data["usuarios"][user_id].get("activo"):
            # Validación de horario: 4:00 PM a 6:00 PM (16:00 a 18:00)
            ahora = datetime.now().time()
            inicio_horario = time(16, 0)
            fin_horario = time(18, 0)

            if not (inicio_horario <= ahora <= fin_horario):
                await update.message.reply_text("⏳ Los retiros solo están permitidos en el horario de **4:00 PM a 6:00 PM**. Por favor, intenta en ese horario.", parse_mode="Markdown", reply_markup=obtener_menu_principal(is_reg))
                return

            dia = datetime.now().weekday()
            if dia not in [3, 4]: # Jueves y Viernes
                await update.message.reply_text("⚠️ Los retiros son jueves y viernes de 4 PM a 6 PM.", reply_markup=obtener_menu_principal(is_reg))
                return

            u_info = data["usuarios"][user_id]
            if es_misma_semana(u_info.get("ultimo_retiro_fecha", "")):
                await update.message.reply_text("⚠️ Ya realizaste tu retiro esta semana.", reply_markup=obtener_menu_principal(is_reg))
                return

            data.setdefault("estados_retiro", {})[user_id] = {"fase": "monto"}
            guardar_data(data)
            await update.message.reply_text("📤 Escribe el monto a retirar:", reply_markup=obtener_menu_principal(is_reg))
        else:
            await update.message.reply_text("⚠️ Tu cuenta debe estar activa.", reply_markup=obtener_menu_principal(is_reg))

    elif texto == '💎 Planes':
        if is_reg:
            await update.message.reply_text("💎 Selecciona el paquete:", reply_markup=obtener_teclado_paquetes())

    elif texto == '👥 Invitar Amigo':
        if is_reg:
            link_bot = f"https://t.me/{context.bot.username}?start={user_id}"
            await update.message.reply_text(
                f"👥 *SISTEMA DE INVITACIONES*\n\n"
                f"Comparte tu enlace de invitación con tus amigos:\n[Enlace de Invitación]({link_bot})\n\n"
                f"¡Invítalos a formar parte de la plataforma!",
                parse_mode="Markdown", reply_markup=obtener_menu_principal(True)
            )

    elif texto == 'ℹ️ Información':
        texto_info = (
            "ℹ️ *INFORMACIÓN Y REGLAS DE LA PLATAFORMA* ℹ️\n\n"
            f"• *Rendimiento:* Generamos un {PORCENTAJE_DIARIO}% diario sobre tu capital depositado.\n"
            f"• *Límite del Paquete:* Cada paquete de inversión tiene validez hasta alcanzar el *200% de retorno* sobre la inversión inicial.\n\n"
            "🗓 *CRONOGRAMA Y COMISIONES DE RETIRO:*\n"
            "• *Días:* Jueves y viernes de cada semana (1 retiro per semana).\n"
            "• *Comisión Ganancias:* 1% por retiro de saldo generado.\n"
            "• *Comisión Depósito:* 2% por retiro de saldo depositado.\n"
            "• *Retiro de Depósito:* Solo permitido antes de haber comenzado a generar ganancias (al retirarlo, se cancela la participación).\n"
            f"• *Mínimo de Retiro:* {MIN_RETIRO} USDT.\n\n"
            "⚠️ *NOTAS IMPORTANTES:*\n"
            "1. Asegúrate de enviar el comprobante de depósito al privado del administrador para procesar tu activación.\n"
            "2. Toda inversión conlleva riesgos.\n\n"
            f"¿Tienes dudas adicionales? Contacta a soporte a través de {ADMIN_USERNAME}"
        )
        await update.message.reply_text(texto_info, parse_mode="Markdown", reply_markup=obtener_menu_principal(is_reg))

async def boton_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data_cb = query.data
    user_id = str(query.from_user.id)
    data = obtener_data()

    if data_cb.startswith("act_"):
        target_id = data_cb.split("_")[1]
        data["usuarios"][target_id]["activo"] = True
        data["usuarios"][target_id]["fecha_activacion"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # Comisión de referido (2% del primer paquete)
        if "referidos" in data and target_id in data["referidos"]:
            ref_id = data["referidos"][target_id]
            if ref_id in data["usuarios"]:
                monto_paq = data["usuarios"][target_id]["deposito"]
                comision_ref = monto_paq * 0.02
                data["usuarios"][ref_id]["ganancias_acumuladas"] = data["usuarios"][ref_id].get("ganancias_acumuladas", 0) + comision_ref
                try:
                    await context.bot.send_message(int(ref_id), f"🎉 ¡Has ganado {comision_ref:.2f} USDT de comisión por el paquete de tu invitado!")
                except:
                    pass

        guardar_data(data)
        await context.bot.send_message(int(target_id), "🎉 ¡Cuenta ACTIVADA con éxito!", reply_markup=obtener_menu_principal(True))
        await query.edit_message_text(f"✅ Usuario {target_id} activado.")
        return

    elif data_cb.startswith("rej_"):
        target_id = data_cb.split("_")[1]
        await context.bot.send_message(int(target_id), "❌ Solicitud rechazada.", reply_markup=obtener_menu_principal(False))
        await query.edit_message_text(f"❌ Usuario {target_id} rechazado.")
        return

    if data_cb.startswith("paq_"):
        monto = float(data_cb.split("_")[1])
        if user_id in data.get("estados_registro", {}):
            reg = data["estados_registro"][user_id]
            data["usuarios"][user_id] = {
                "nombre": reg["nombre"], "email": reg["email"], "telefono": reg["telefono"],
                "deposito": monto, "ganancias_acumuladas": 0.0, "total_generado": 0.0,
                "ultimo_retiro_fecha": "", "activo": False
            }
            del data["estados_registro"][user_id]
            guardar_data(data)
            await query.message.edit_text(f"✅ Paquete de {monto} USDT seleccionado. Envía el comprobante a {ADMIN_USERNAME}")
            keyboard = [[InlineKeyboardButton("✅ Activar", callback_data=f"act_{user_id}"), InlineKeyboardButton("❌ Rechazar", callback_data=f"rej_{user_id})")]]
            await context.bot.send_message(ADMIN_TELEGRAM_ID, f"🚨 *NUEVO REGISTRO*\n👤 {reg['nombre']}\n💵 Paquete: {monto}", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
            return

    if data_cb.startswith("ret_"):
        if user_id != str(ADMIN_TELEGRAM_ID): return
        target_id = data_cb.split("_")[1]
        data["estados_admin_retiro"][user_id] = target_id
        guardar_data(data)
        await query.message.reply_text("📸 Envía la captura del comprobante de pago:")
        return

async def tarea_ganancias_diarias():
    while True:
        await asyncio.sleep(86400)
        data = obtener_data()
        for uid, info in data.get("usuarios", {}).items():
            if info.get("activo", False):
                dep = info.get("deposito", 0)
                meta = dep * 2.0
                gen = info.get("total_generado", 0.0)
                if gen < meta:
                    g_hoy = dep * (PORCENTAJE_DIARIO / 100)
                    if gen + g_hoy > meta: g_hoy = meta - gen
                    info["ganancias_acumuladas"] = info.get("ganancias_acumuladas", 0) + g_hoy
                    info["total_generado"] = gen + g_hoy
        guardar_data(data)

async def tarea_corte_18pm(application):
    while True:
        ahora = datetime.now()
        objetivo = ahora.replace(hour=18, minute=0, second=0, microsecond=0)
        if ahora >= objetivo:
            objetivo += timedelta(days=1)
        
        segundos_espera = (objetivo - ahora).total_seconds()
        await asyncio.sleep(segundos_espera)

        data = obtener_data()
        solicitudes = data.get("solicitudes_hoy", [])
        
        if solicitudes:
            total_usdt = sum(s["neto"] for s in solicitudes)
            detalle = "\n".join([f"• {s['usuario']}: {s['neto']:.2f} USDT" for s in solicitudes])
            
            reporte = (
                f"📊 *REPORTE AUTOMÁTICO DE CIERRE (6:00 PM)*\n\n"
                f"💵 *Total acumulado a retirar:* {total_usdt:.2f} USDT\n\n"
                f"👤 *Detalle de usuarios:*\n{detalle}"
            )
            try:
                await application.bot.send_message(ADMIN_TELEGRAM_ID, reporte, parse_mode="Markdown")
            except Exception as e:
                print(f"Error enviando reporte automático: {e}")
            
            data["solicitudes_hoy"] = []
            guardar_data(data)

async def main():
    await start_web_server()
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CallbackQueryHandler(boton_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, manejar_mensajes_texto))

    asyncio.create_task(tarea_ganancias_diarias())
    asyncio.create_task(tarea_corte_18pm(app))

    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    
    stop_signal = asyncio.Event()
    try:
        await stop_signal.wait()
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
