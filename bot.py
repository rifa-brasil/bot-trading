import os
import sqlite3
import shutil
import uuid
import asyncio
from io import BytesIO
from urllib.parse import quote
from zoneinfo import ZoneInfo
from openpyxl import Workbook
from datetime import datetime, timedelta, timezone

from aiohttp import web
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    InputFile,
)
from telegram.constants import ChatType
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    filters,
)

# =========================================================
# CONFIGURACIÓN
# =========================================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
ADMIN_TELEGRAM_ID = int(os.getenv("ADMIN_TELEGRAM_ID", "0") or "0")

# Wallet TRC20/USDT que recibirán los depósitos.
# CONFIGÚRALA EN RENDER COMO VARIABLE DE ENTORNO.
USDT_TRC20_ADDRESS = os.getenv("USDT_TRC20_ADDRESS", "").strip()

DB_FILE = "database.db"
BACKUP_DIR = "backups"

# Plan de prueba/configurable.
# No representa una promesa de rentabilidad: son parámetros del sistema.
PLAN_NAME = os.getenv("PLAN_NAME", "Plan Inicial")
DAILY_RATE = 0.005  # 0,5% diario
BACKUP_TIME = os.getenv("BACKUP_TIME", "06:00").strip()
BACKUP_TIMEZONE = os.getenv("BACKUP_TIMEZONE", "America/Sao_Paulo").strip()
TARGET_MULTIPLIER = float(os.getenv("TARGET_MULTIPLIER", "2.0"))
MIN_INVESTMENT = 50.0  # inversión mínima: 50 USDT
MIN_WITHDRAWAL = 15.0  # retiro mínimo: 15 USDT
WITHDRAWAL_INTERVAL_DAYS = 7
MAX_INVESTMENT = float(os.getenv("MAX_INVESTMENT", "1000000"))

# Planes de inversión disponibles. El monto del plan queda definido por el botón.
INVESTMENT_PLANS = [50, 100, 120, 150, 200, 250, 300, 350, 500, 650, 1000, 1500, 2000]

# Estados de conversación
DEP_AMOUNT, DEP_TX, DEP_PHOTO = range(3)
WITHDRAW_AMOUNT, WITHDRAW_ADDRESS = range(3, 5)

# =========================================================
# WEB SERVER PARA RENDER
# =========================================================

async def handle_web(request):
    return web.Response(text="Bot de Inversión activo y en línea.")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_web)

    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.environ.get("PORT", "10000"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    print(f"🌐 Servidor web corriendo en el puerto {port}")


# =========================================================
# BASE DE DATOS
# =========================================================

def db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS usuarios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER UNIQUE NOT NULL,
            nombre TEXT NOT NULL DEFAULT '',
            username TEXT NOT NULL DEFAULT '',
            saldo REAL NOT NULL DEFAULT 0,
            invertido REAL NOT NULL DEFAULT 0,
            ganancias REAL NOT NULL DEFAULT 0,
            total_depositado REAL NOT NULL DEFAULT 0,
            total_retirado REAL NOT NULL DEFAULT 0,
            codigo_referido TEXT UNIQUE,
            referido_por TEXT,
            fecha_registro TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS depositos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL,
            monto REAL NOT NULL,
            tx_hash TEXT NOT NULL DEFAULT '',
            foto_file_id TEXT NOT NULL DEFAULT '',
            estado TEXT NOT NULL DEFAULT 'pendiente',
            fecha TEXT NOT NULL,
            fecha_revision TEXT,
            revisado_por INTEGER
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS retiros (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL,
            monto REAL NOT NULL,
            direccion TEXT NOT NULL,
            estado TEXT NOT NULL DEFAULT 'pendiente',
            fecha TEXT NOT NULL,
            fecha_revision TEXT,
            revisado_por INTEGER
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS inversiones (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL,
            plan TEXT NOT NULL,
            capital REAL NOT NULL,
            ganancia_acumulada REAL NOT NULL DEFAULT 0,
            tasa_diaria REAL NOT NULL,
            multiplicador_objetivo REAL NOT NULL,
            estado TEXT NOT NULL DEFAULT 'activa',
            fecha_inicio TEXT NOT NULL,
            ultimo_calculo TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS movimientos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL,
            tipo TEXT NOT NULL,
            monto REAL NOT NULL DEFAULT 0,
            descripcion TEXT NOT NULL DEFAULT '',
            fecha TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS referidos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            referido_id INTEGER NOT NULL,
            referidor_id INTEGER NOT NULL,
            nivel INTEGER NOT NULL DEFAULT 1,
            bono REAL NOT NULL DEFAULT 0,
            fecha TEXT NOT NULL,
            UNIQUE(referido_id, referidor_id)
        )
    """)

    cur.execute("UPDATE inversiones SET tasa_diaria = ? WHERE estado = 'activa'", (DAILY_RATE,))
    conn.commit()
    conn.close()


# =========================================================
# UTILIDADES
# =========================================================

def money(value):
    return f"{float(value):,.2f}"


def is_admin(user_id):
    return ADMIN_TELEGRAM_ID != 0 and user_id == ADMIN_TELEGRAM_ID


def private_only(update):
    chat = update.effective_chat
    return chat is not None and chat.type == ChatType.PRIVATE


def generate_ref_code(telegram_id):
    return f"ref{telegram_id}"


def get_bot_username(context):
    return context.application.bot_data.get("bot_username", "")


def get_user(telegram_id):
    conn = db()
    row = conn.execute(
        "SELECT * FROM usuarios WHERE telegram_id = ?",
        (telegram_id,)
    ).fetchone()
    conn.close()
    return row


def ensure_user(user, start_ref=None):
    conn = db()
    existing = conn.execute(
        "SELECT * FROM usuarios WHERE telegram_id = ?",
        (user.id,)
    ).fetchone()

    if existing:
        conn.execute("""
            UPDATE usuarios
            SET nombre = ?, username = ?
            WHERE telegram_id = ?
        """, (
            user.full_name or "",
            user.username or "",
            user.id
        ))
        conn.commit()
        conn.close()
        return

    referido_por = None
    if start_ref:
        candidate = start_ref.strip()
        referrer = conn.execute(
            "SELECT telegram_id FROM usuarios WHERE codigo_referido = ?",
            (candidate,)
        ).fetchone()

        if referrer and int(referrer["telegram_id"]) != user.id:
            referido_por = candidate

    ref_code = generate_ref_code(user.id)

    conn.execute("""
        INSERT INTO usuarios (
            telegram_id, nombre, username, codigo_referido,
            referido_por, fecha_registro
        )
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        user.id,
        user.full_name or "",
        user.username or "",
        ref_code,
        referido_por,
        now_iso()
    ))

    if referido_por:
        referrer = conn.execute(
            "SELECT telegram_id FROM usuarios WHERE codigo_referido = ?",
            (referido_por,)
        ).fetchone()

        if referrer:
            conn.execute("""
                INSERT OR IGNORE INTO referidos (
                    referido_id, referidor_id, nivel, bono, fecha
                )
                VALUES (?, ?, 1, 0, ?)
            """, (
                user.id,
                referrer["telegram_id"],
                now_iso()
            ))

    conn.commit()
    conn.close()


def add_movement(telegram_id, tipo, amount, description):
    conn = db()
    conn.execute("""
        INSERT INTO movimientos (
            telegram_id, tipo, monto, descripcion, fecha
        )
        VALUES (?, ?, ?, ?, ?)
    """, (
        telegram_id, tipo, amount, description, now_iso()
    ))
    conn.commit()
    conn.close()


def withdrawal_wait_info(telegram_id):
    """Devuelve (puede_retirar, segundos_restantes) usando retiros pendientes/aprobados."""
    conn = db()
    row = conn.execute("""
        SELECT fecha
        FROM retiros
        WHERE telegram_id = ?
          AND estado IN ('pendiente', 'aprobado')
        ORDER BY id DESC
        LIMIT 1
    """, (telegram_id,)).fetchone()
    conn.close()
    if not row or not row["fecha"]:
        return True, 0
    try:
        last = datetime.fromisoformat(row["fecha"])
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
    except Exception:
        return True, 0
    elapsed = (datetime.now(timezone.utc) - last).total_seconds()
    remaining = max(0, WITHDRAWAL_INTERVAL_DAYS * 86400 - elapsed)
    return remaining <= 0, int(remaining)


def withdrawal_wait_text(seconds):
    days = seconds // 86400
    hours = (seconds % 86400) // 3600
    minutes = (seconds % 3600) // 60
    if days > 0:
        return f"{days} día(s) y {hours} hora(s)"
    if hours > 0:
        return f"{hours} hora(s) y {minutes} minuto(s)"
    return f"{minutes} minuto(s)"


def user_balance(telegram_id):
    row = get_user(telegram_id)
    if not row:
        return 0.0
    return float(row["saldo"])


# =========================================================
# MENÚS
# =========================================================

def user_keyboard():
    return ReplyKeyboardMarkup([
        ["👤 Mi cuenta", "📈 Inversiones"],
        ["💎 Planes de Inversión"],
        ["💰 Depositar", "💸 Retirar"],
        ["🤝 Referidos", "📜 Historial"],
        ["ℹ️ Información"],
    ], resize_keyboard=True, is_persistent=True)


def admin_keyboard():
    return ReplyKeyboardMarkup([
        ["👥 Usuarios", "📥 Depósitos"],
        ["📤 Retiros", "📈 Inversiones"],
        ["💾 Crear respaldo", "📊 Estado"],
        ["⚙️ Procesar ganancias"],
        ["👤 Menú usuario"],
    ], resize_keyboard=True, is_persistent=True)


def back_inline(callback="admin_home"):
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Panel", callback_data=callback)]])


async def send_user_menu(chat_id, context, text=None):
    if text is None:
        text = "🏦 *MENÚ PRINCIPAL*\n\nSelecciona una opción:"
    await context.bot.send_message(
        chat_id=chat_id, text=text, reply_markup=user_keyboard(), parse_mode="Markdown"
    )


async def send_admin_menu(chat_id, context, text=None):
    if text is None:
        text = "👑 *PANEL DE ADMINISTRACIÓN*\n\nSelecciona una opción:"
    await context.bot.send_message(
        chat_id=chat_id, text=text, reply_markup=admin_keyboard(), parse_mode="Markdown"
    )


# =========================================================
# /START
# =========================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not private_only(update):
        return

    user = update.effective_user

    start_ref = None
    if context.args:
        start_ref = context.args[0]

    ensure_user(user, start_ref)

    if is_admin(user.id):
        await send_admin_menu(
            user.id,
            context,
            "👑 *PANEL DE ADMINISTRACIÓN*\n\n"
            "También puedes entrar al menú normal con el botón correspondiente."
        )
    else:
        await send_user_menu(
            user.id,
            context,
            f"👋 *Bienvenido, {user.first_name or 'usuario'}*\n\n"
            "Desde aquí puedes administrar tu cuenta, depositar, "
            "solicitar retiros, consultar inversiones y compartir tu enlace de referido."
        )


# =========================================================
# /ADMIN
# =========================================================

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not private_only(update):
        return

    user = update.effective_user

    if not is_admin(user.id):
        await update.message.reply_text("⛔ No autorizado.")
        return

    await send_admin_menu(user.id, context)


# =========================================================
# CUENTA
# =========================================================

async def show_account(query):
    user_id = query.from_user.id
    # Reparación: si por alguna razón el registro no existe, se crea/actualiza aquí.
    row = get_user(user_id)
    if not row:
        ensure_user(query.from_user)
        row = get_user(user_id)

    if not row:
        return await query.edit_message_text(
            "⚠️ No se pudo cargar tu cuenta. Pulsa /start y vuelve a intentarlo.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menú Principal", callback_data="user_home")]])
        )

    conn = db()
    total_depositado_db = conn.execute(
        "SELECT COALESCE(SUM(monto), 0) AS s FROM depositos WHERE telegram_id=? AND estado='aprobado'",
        (user_id,)
    ).fetchone()["s"]
    total_retirado_db = conn.execute(
        "SELECT COALESCE(SUM(monto), 0) AS s FROM retiros WHERE telegram_id=? AND estado='aprobado'",
        (user_id,)
    ).fetchone()["s"]
    conn.close()
    total_depositado = max(float(row["total_depositado"] or 0), float(total_depositado_db or 0))
    total_retirado = max(float(row["total_retirado"] or 0), float(total_retirado_db or 0))
    ganancias = float(row["ganancias"] or 0)
    porcentaje = (ganancias / total_depositado * 100) if total_depositado > 0 else 0.0

    texto = (
        "👤 *MI CUENTA*\n\n"
        f"🆔 ID: `{row['telegram_id']}`\n"
        f"👤 Nombre: {row['nombre'] or '-'}\n"
        f"💰 Saldo disponible: *{money(row['saldo'])} USDT*\n"
        f"📥 Total depositado: *{money(total_depositado)} USDT*\n"
        f"📈 Capital invertido: *{money(row['invertido'])} USDT*\n"
        f"💵 Ganancias acumuladas: *{money(ganancias)} USDT*\n"
        f"📊 Ganancia de la cuenta: *{porcentaje:.2f}%*\n"
        f"📤 Total retirado: *{money(total_retirado)} USDT*\n\n"
        "El saldo disponible es el dinero acreditado que todavía no está invertido y puede utilizarse según los planes disponibles."
    )

    await query.edit_message_text(
        texto,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("💎 Planes de Inversión", callback_data="user_plans")],
            [InlineKeyboardButton("⬅️ Atrás", callback_data="user_home"), InlineKeyboardButton("🏠 Menú Principal", callback_data="user_home")]
        ])
    )


# =========================================================
# REFERIDOS
# =========================================================

async def show_referrals(query, context):
    user_id = query.from_user.id
    row = get_user(user_id)

    if not row:
        return

    username = get_bot_username(context)
    ref_code = row["codigo_referido"] or generate_ref_code(user_id)

    link = f"https://t.me/{username}?start={ref_code}" if username else (
        f"Abre el bot y usa tu código: {ref_code}"
    )

    conn = db()
    count = conn.execute(
        "SELECT COUNT(*) AS c FROM referidos WHERE referidor_id = ?",
        (user_id,)
    ).fetchone()["c"]

    bonus = conn.execute(
        "SELECT COALESCE(SUM(bono), 0) AS total "
        "FROM referidos WHERE referidor_id = ?",
        (user_id,)
    ).fetchone()["total"]
    conn.close()

    share_text = (
        "Únete a este bot para conocer el sistema de inversión:\n\n"
        f"{link}"
    )

    share_url = (
        "https://t.me/share/url?"
        f"url={link.replace(' ', '%20')}"
        f"&text={share_text.replace(' ', '%20')}"
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 Compartir mi enlace", url=share_url)],
        [InlineKeyboardButton("⬅️ Atrás", callback_data="user_home"), InlineKeyboardButton("🏠 Menú Principal", callback_data="user_home")],
    ])

    texto = (
        "🤝 *PROGRAMA DE REFERIDOS*\n\n"
        "Tu enlace personal:\n"
        f"`{link}`\n\n"
        f"👥 Personas registradas: *{count}*\n"
        f"🎁 Bonos acumulados: *{money(bonus)} USDT*\n\n"
        "Comparte el enlace para que otros puedan registrarse usando tu referencia."
    )

    await query.edit_message_text(
        texto,
        parse_mode="Markdown",
        reply_markup=keyboard
    )


# =========================================================
# INFORMACIÓN
# =========================================================

async def show_info(query):
    texto = (
        "ℹ️ *INFORMACIÓN DEL SISTEMA*\n\n"
        f"📊 Tasa diaria fija: *{DAILY_RATE * 100:.1f}%*\n"
        "🎯 Cada plan permanece activo hasta alcanzar un rendimiento total equivalente al *200% del capital del plan* (capital + ganancias).\n"
        f"💵 Inversión mínima: *{money(MIN_INVESTMENT)} USDT*\n"
        f"💸 Retiro mínimo: *{money(MIN_WITHDRAWAL)} USDT*\n"
        "🗓️ Frecuencia de retiros: *1 solicitud cada 7 días*\n\n"
        "🌐 Red de depósitos y retiros: *TRC20*\n"
        "📥 Los depósitos son revisados manualmente por el administrador.\n"
        "📤 Los retiros también son revisados manualmente."
    )

    await query.edit_message_text(
        texto,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Atrás", callback_data="user_home"), InlineKeyboardButton("🏠 Menú Principal", callback_data="user_home")]
        ])
    )


# =========================================================
# PLANES DE INVERSIÓN
# =========================================================

async def show_plans(query):
    user_id = query.from_user.id
    row = get_user(user_id)
    if not row:
        ensure_user(query.from_user)
        row = get_user(user_id)
    balance = float(row["saldo"]) if row else 0.0

    buttons = []
    for amount in INVESTMENT_PLANS:
        buttons.append([InlineKeyboardButton(
            f"💎 Plan {money(amount)} USDT",
            callback_data=f"plan_{amount}"
        )])

    buttons.append([
        InlineKeyboardButton("⬅️ Atrás", callback_data="user_home"),
        InlineKeyboardButton("🏠 Menú Principal", callback_data="user_home")
    ])

    texto = (
        "💎 *PLANES DE INVERSIÓN*\n\n"
        f"Saldo disponible: *{money(balance)} USDT*\n"
        f"Tasa diaria fija: *{DAILY_RATE * 100:.1f}%*\n"
        "Selecciona el plan que deseas contratar."
    )
    await query.edit_message_text(texto, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))


async def select_plan(query, amount):
    user_id = query.from_user.id
    row = get_user(user_id)
    if not row:
        ensure_user(query.from_user)
        row = get_user(user_id)
    balance = float(row["saldo"]) if row else 0.0

    if amount not in INVESTMENT_PLANS:
        await query.answer("Plan no disponible.", show_alert=True)
        return

    if balance >= amount:
        await query.edit_message_text(
            "🚀 *PLAN DISPONIBLE PARA INVERTIR*\n\n"
            f"Plan seleccionado: *{money(amount)} USDT*\n"
            f"Saldo disponible: *{money(balance)} USDT*\n"
            f"Tasa diaria fija: *{DAILY_RATE * 100:.1f}%*\n"
            "El plan permanecerá activo hasta alcanzar el 200% del capital del plan.\n\n"
            "Puedes invertir ahora usando exactamente el monto de este plan.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"✅ Invertir {money(amount)} USDT", callback_data=f"confirm_plan_{amount}")],
                [InlineKeyboardButton("⬅️ Atrás", callback_data="user_plans"), InlineKeyboardButton("🏠 Menú Principal", callback_data="user_home")]
            ])
        )
    else:
        faltante = amount - balance
        await query.edit_message_text(
            "💰 *DEPÓSITO PARA ESTE PLAN*\n\n"
            f"Plan seleccionado: *{money(amount)} USDT*\n"
            f"Saldo disponible: *{money(balance)} USDT*\n"
            f"Falta depositar: *{money(faltante)} USDT*\n\n"
            "Al pulsar el botón de abajo, el monto del depósito queda fijado al valor del plan. No tendrás que escribir la cantidad.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"💰 Depositar {money(faltante)} USDT", callback_data=f"deposit_plan_{amount}")],
                [InlineKeyboardButton("⬅️ Atrás", callback_data="user_plans"), InlineKeyboardButton("🏠 Menú Principal", callback_data="user_home")]
            ])
        )


async def confirm_plan_investment(query, amount):
    user_id = query.from_user.id
    conn = db()
    row = conn.execute("SELECT saldo FROM usuarios WHERE telegram_id = ?", (user_id,)).fetchone()
    if not row:
        conn.close()
        await query.answer("Cuenta no encontrada.", show_alert=True)
        return
    balance = float(row["saldo"])
    if balance < amount:
        conn.close()
        await query.answer("El saldo disponible ya no alcanza para este plan.", show_alert=True)
        return

    now = now_iso()
    conn.execute("""
        UPDATE usuarios SET saldo = saldo - ?, invertido = invertido + ?
        WHERE telegram_id = ?
    """, (amount, amount, user_id))
    conn.execute("""
        INSERT INTO inversiones (
            telegram_id, plan, capital, ganancia_acumulada,
            tasa_diaria, multiplicador_objetivo, estado,
            fecha_inicio, ultimo_calculo
        ) VALUES (?, ?, ?, 0, ?, 2.0, 'activa', ?, ?)
    """, (user_id, f"Plan {money(amount)} USDT", amount, DAILY_RATE, now, now))
    conn.commit()
    conn.close()

    add_movement(user_id, "inversion", amount, f"Inversión creada: Plan {money(amount)} USDT")

    await query.edit_message_text(
        "✅ *INVERSIÓN CREADA*\n\n"
        f"Plan: *{money(amount)} USDT*\n"
        f"Capital invertido: *{money(amount)} USDT*\n"
        f"Tasa diaria fija: *{DAILY_RATE * 100:.1f}%*\n"
        "Objetivo del plan: alcanzar el 200% del capital total del plan.\n\n"
        "Tu saldo disponible se actualizó correctamente.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📈 Ver inversiones", callback_data="user_invest")],
            [InlineKeyboardButton("🏠 Menú Principal", callback_data="user_home")]
        ])
    )


async def start_plan_deposit(query, context, plan_amount):
    row = get_user(query.from_user.id)
    balance = float(row["saldo"]) if row else 0.0
    deposit_amount = max(0.0, float(plan_amount) - balance)
    context.user_data.clear()
    context.user_data["manual_flow"] = "deposit_tx_fixed"
    context.user_data["deposit_amount"] = deposit_amount
    await query.edit_message_text(
        "💰 *DEPÓSITO DEL PLAN*\n\n"
        f"Plan seleccionado: *{money(plan_amount)} USDT*\n"
        f"Saldo disponible actual: *{money(balance)} USDT*\n"
        f"Monto exacto a depositar: *{money(deposit_amount)} USDT*\n\n"
        f"Wallet USDT TRC20:\n`{USDT_TRC20_ADDRESS or 'NO CONFIGURADA'}`\n\n"
        "Realiza el depósito por el monto indicado y después envía aquí el TXID. No necesitas escribir el monto.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Atrás", callback_data="user_plans"), InlineKeyboardButton("🏠 Menú Principal", callback_data="user_home")]
        ])
    )


# =========================================================
# INVERSIONES
# =========================================================

def investment_summary(telegram_id):
    conn = db()
    rows = conn.execute("""
        SELECT *
        FROM inversiones
        WHERE telegram_id = ?
        ORDER BY id DESC
    """, (telegram_id,)).fetchall()
    conn.close()
    return rows


async def show_investments(query):
    user_id = query.from_user.id
    row = get_user(user_id)
    if not row:
        ensure_user(query.from_user)
        row = get_user(user_id)
    rows = investment_summary(user_id)
    conn = db()
    total_deposited = conn.execute("SELECT COALESCE(SUM(monto), 0) AS s FROM depositos WHERE telegram_id=? AND estado='aprobado'", (user_id,)).fetchone()["s"]
    active_invested = conn.execute("SELECT COALESCE(SUM(capital), 0) AS s FROM inversiones WHERE telegram_id=? AND estado='activa'", (user_id,)).fetchone()["s"]
    total_gains = conn.execute("SELECT COALESCE(SUM(ganancia_acumulada), 0) AS s FROM inversiones WHERE telegram_id=?", (user_id,)).fetchone()["s"]
    conn.close()
    daily = float(active_invested) * DAILY_RATE
    balance = float(row["saldo"]) if row else 0.0
    pct = (float(total_gains) / float(total_deposited) * 100) if float(total_deposited) > 0 else 0.0

    lines = [
        "📈 *MIS INVERSIONES*", "",
        f"💰 Total depositado aprobado: *{money(total_deposited)} USDT*",
        f"💳 Saldo disponible para invertir: *{money(balance)} USDT*",
        f"📊 Capital actualmente invertido: *{money(active_invested)} USDT*",
        f"💵 Ganancia acumulada: *{money(total_gains)} USDT*",
        f"📈 Rendimiento de la cuenta: *{pct:.2f}%*",
        f"💵 Ganancia diaria estimada al {DAILY_RATE * 100:.1f}%: *{money(daily)} USDT*", ""
    ]
    if rows:
        for inv in rows:
            capital = float(inv["capital"])
            gain = float(inv["ganancia_acumulada"])
            target_profit = capital
            progress = min(100.0, gain / target_profit * 100) if target_profit else 0.0
            total_value = capital + gain
            lines.append(
                f"💎 *{inv['plan']}*\n"
                f"Capital: {money(capital)} USDT\n"
                f"Ganancia: {money(gain)} USDT\n"
                f"Total generado: {money(total_value)} USDT\n"
                f"Progreso hacia el 200%: {progress:.2f}%\n"
                f"Estado: {inv['estado']}\n"
            )
    else:
        lines.append("No tienes inversiones registradas todavía.")

    buttons = []
    if balance >= MIN_INVESTMENT:
        buttons.append([InlineKeyboardButton("🚀 Invertir saldo disponible", callback_data="user_new_investment")])
    buttons.append([InlineKeyboardButton("💎 Planes de Inversión", callback_data="user_plans")])
    buttons.append([InlineKeyboardButton("⬅️ Atrás", callback_data="user_home"), InlineKeyboardButton("🏠 Menú Principal", callback_data="user_home")])
    await query.edit_message_text("\n".join(lines), parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))

async def create_investment(query):
    user_id = query.from_user.id
    row = get_user(user_id)

    if not row:
        ensure_user(query.from_user)
        row = get_user(user_id)
    if not row:
        await query.edit_message_text("⚠️ No se pudo cargar tu cuenta.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menú Principal", callback_data="user_home")]]))
        return

    balance = float(row["saldo"])

    if balance < MIN_INVESTMENT:
        await query.edit_message_text(
            "⚠️ *SALDO INSUFICIENTE*\n\n"
            f"Saldo disponible: {money(balance)} USDT\n"
            f"Mínimo para invertir: {money(MIN_INVESTMENT)} USDT\n\n"
            "Primero realiza un depósito y espera su aprobación.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💰 Depositar", callback_data="user_deposit")],
                [InlineKeyboardButton("⬅️ Volver", callback_data="user_invest")]
            ])
        )
        return

    # Para evitar inversiones accidentales, mostramos confirmación.
    await query.edit_message_text(
        "🚀 *CONFIRMAR INVERSIÓN*\n\n"
        f"Plan: *Saldo disponible completo*\n"
        f"Saldo disponible: *{money(balance)} USDT*\n"
        f"Tasa configurada: *{DAILY_RATE * 100:.4g}% diaria*\n"
        f"Objetivo configurado: *{TARGET_MULTIPLIER:.2f}x*\n\n"
        "La inversión utilizará el saldo disponible completo.\n\n"
        "⚠️ Estos son parámetros del sistema, no una garantía de resultado.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(
                "✅ Confirmar inversión",
                callback_data="user_confirm_invest"
            )],
            [InlineKeyboardButton("❌ Cancelar", callback_data="user_invest")]
        ])
    )


async def confirm_investment(query):
    user_id = query.from_user.id
    conn = db()

    row = conn.execute(
        "SELECT saldo FROM usuarios WHERE telegram_id = ?",
        (user_id,)
    ).fetchone()

    if not row:
        conn.close()
        return

    amount = float(row["saldo"])

    if amount < MIN_INVESTMENT:
        conn.close()
        await query.answer(
            "Saldo insuficiente.",
            show_alert=True
        )
        return

    if amount > MAX_INVESTMENT:
        amount = MAX_INVESTMENT

    conn.execute("""
        UPDATE usuarios
        SET saldo = saldo - ?, invertido = invertido + ?
        WHERE telegram_id = ?
    """, (amount, amount, user_id))

    conn.execute("""
        INSERT INTO inversiones (
            telegram_id, plan, capital, ganancia_acumulada,
            tasa_diaria, multiplicador_objetivo,
            estado, fecha_inicio, ultimo_calculo
        )
        VALUES (?, ?, ?, 0, ?, ?, 'activa', ?, ?)
    """, (
        user_id,
        PLAN_NAME,
        amount,
        DAILY_RATE,
        TARGET_MULTIPLIER,
        now_iso(),
        now_iso()
    ))

    conn.commit()
    conn.close()

    add_movement(
        user_id,
        "inversion",
        amount,
        f"Inversión creada: {PLAN_NAME}"
    )

    await query.edit_message_text(
        "✅ *INVERSIÓN CREADA*\n\n"
        f"Plan: *{PLAN_NAME}*\n"
        f"Capital: *{money(amount)} USDT*\n"
        f"Tasa configurada: *{DAILY_RATE * 100:.4g}% diaria*\n\n"
        "La inversión ya aparece en tu panel.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📈 Ver inversiones", callback_data="user_invest")],
            [InlineKeyboardButton("🏠 Menú principal", callback_data="user_home")]
        ])
    )


# =========================================================
# GANANCIAS
# =========================================================

def process_profits():
    """
    Calcula ganancias acumuladas desde el último cálculo.
    El proceso se ejecuta al presionar el botón administrativo.
    """
    conn = db()

    rows = conn.execute("""
        SELECT *
        FROM inversiones
        WHERE estado = 'activa'
    """).fetchall()

    processed = 0
    total_profit = 0.0

    now = datetime.now(timezone.utc)

    for inv in rows:
        try:
            last = datetime.fromisoformat(inv["ultimo_calculo"])
        except Exception:
            last = now

        elapsed_seconds = max(0, (now - last).total_seconds())
        days = elapsed_seconds / 86400.0

        if days <= 0:
            continue

        capital = float(inv["capital"])
        accumulated = float(inv["ganancia_acumulada"])
        target_profit = capital * (float(inv["multiplicador_objetivo"]) - 1)

        available_target = max(0.0, target_profit - accumulated)

        if available_target <= 0:
            conn.execute("""
                UPDATE inversiones
                SET estado = 'completada', ultimo_calculo = ?
                WHERE id = ?
            """, (now.isoformat(), inv["id"]))
            continue

        profit = capital * float(inv["tasa_diaria"]) * days
        profit = min(profit, available_target)

        if profit <= 0:
            continue

        conn.execute("""
            UPDATE inversiones
            SET ganancia_acumulada = ganancia_acumulada + ?,
                ultimo_calculo = ?
            WHERE id = ?
        """, (profit, now.isoformat(), inv["id"]))

        conn.execute("""
            UPDATE usuarios
            SET ganancias = ganancias + ?,
                saldo = saldo + ?
            WHERE telegram_id = ?
        """, (profit, profit, inv["telegram_id"]))

        conn.execute("""
            INSERT INTO movimientos (
                telegram_id, tipo, monto, descripcion, fecha
            )
            VALUES (?, 'ganancia', ?, ?, ?)
        """, (
            inv["telegram_id"],
            profit,
            f"Ganancia procesada: inversión #{inv['id']}",
            now.isoformat()
        ))

        new_total = accumulated + profit
        if new_total >= target_profit:
            conn.execute("""
                UPDATE inversiones
                SET estado = 'completada'
                WHERE id = ?
            """, (inv["id"],))

        processed += 1
        total_profit += profit

    conn.commit()
    conn.close()

    return processed, total_profit


# =========================================================
# DEPÓSITOS
# =========================================================

async def start_deposit(update, context):
    if not private_only(update):
        return ConversationHandler.END

    context.user_data.clear()
    await update.message.reply_text(
        "💰 *NUEVO DEPÓSITO*\n\n"
        f"Red: *TRC20*\n"
        f"Wallet:\n`{USDT_TRC20_ADDRESS or 'NO CONFIGURADA'}`\n\n"
        "Envía ahora el monto que vas a depositar en USDT.",
        parse_mode="Markdown"
    )
    return DEP_AMOUNT


async def receive_deposit_amount(update, context):
    if not private_only(update):
        return ConversationHandler.END

    try:
        amount = float(update.message.text.replace(",", ".").strip())
    except ValueError:
        await update.message.reply_text("⚠️ Introduce solamente un número.")
        return DEP_AMOUNT

    if amount <= 0:
        await update.message.reply_text("⚠️ El monto debe ser mayor que 0.")
        return DEP_AMOUNT

    context.user_data["deposit_amount"] = amount

    await update.message.reply_text(
        "🔗 Ahora envía el *hash de la transacción (TXID)*.",
        parse_mode="Markdown"
    )
    return DEP_TX


async def receive_deposit_tx(update, context):
    if not private_only(update):
        return ConversationHandler.END

    tx = update.message.text.strip()

    if len(tx) < 5:
        await update.message.reply_text(
            "⚠️ El TXID parece demasiado corto. Envíalo nuevamente."
        )
        return DEP_TX

    context.user_data["deposit_tx"] = tx

    await update.message.reply_text(
        "📸 Ahora envía una *captura del comprobante* del depósito.\n\n"
        "Si no puedes enviar captura, escribe /skip.",
        parse_mode="Markdown"
    )
    return DEP_PHOTO


async def receive_deposit_photo(update, context):
    if not private_only(update):
        return ConversationHandler.END

    photo = update.message.photo[-1]
    context.user_data["deposit_photo"] = photo.file_id

    return await finish_deposit(update, context)


async def skip_deposit_photo(update, context):
    if not private_only(update):
        return ConversationHandler.END

    context.user_data["deposit_photo"] = ""
    return await finish_deposit(update, context)


async def finish_deposit(update, context):
    user_id = update.effective_user.id
    amount = float(context.user_data.get("deposit_amount", 0))
    tx = context.user_data.get("deposit_tx", "")
    photo_id = context.user_data.get("deposit_photo", "")

    conn = db()
    cur = conn.execute("""
        INSERT INTO depositos (
            telegram_id, monto, tx_hash, foto_file_id,
            estado, fecha
        )
        VALUES (?, ?, ?, ?, 'pendiente', ?)
    """, (
        user_id,
        amount,
        tx,
        photo_id,
        now_iso()
    ))
    deposit_id = cur.lastrowid
    conn.commit()
    conn.close()

    add_movement(
        user_id,
        "deposito_pendiente",
        amount,
        f"Depósito #{deposit_id} enviado para revisión"
    )

    await update.message.reply_text(
        "⏳ *DEPÓSITO ENVIADO*\n\n"
        f"ID: `{deposit_id}`\n"
        f"Monto: *{money(amount)} USDT*\n"
        f"TXID: `{tx}`\n\n"
        "El administrador revisará el depósito manualmente.",
        parse_mode="Markdown"
    )

    admin_text = (
        "📥 *NUEVO DEPÓSITO PENDIENTE*\n\n"
        f"ID: `{deposit_id}`\n"
        f"Usuario: `{user_id}`\n"
        f"Nombre: {update.effective_user.full_name}\n"
        f"Monto: *{money(amount)} USDT*\n"
        f"TXID: `{tx}`"
    )

    admin_keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✅ Aprobar",
                callback_data=f"dep_approve_{deposit_id}"
            ),
            InlineKeyboardButton(
                "❌ Rechazar",
                callback_data=f"dep_reject_{deposit_id}"
            )
        ]
    ])

    try:
        await context.bot.send_message(
            chat_id=ADMIN_TELEGRAM_ID,
            text=admin_text,
            parse_mode="Markdown",
            reply_markup=admin_keyboard
        )

        if photo_id:
            await context.bot.send_photo(
                chat_id=ADMIN_TELEGRAM_ID,
                photo=photo_id,
                caption=f"📸 Comprobante del depósito #{deposit_id}"
            )
    except Exception as e:
        print(f"Error notificando depósito al admin: {e}")

    context.user_data.clear()
    return ConversationHandler.END


async def cancel_deposit(update, context):
    context.user_data.clear()
    await update.message.reply_text("❌ Depósito cancelado.")
    return ConversationHandler.END


# =========================================================
# RETIROS
# =========================================================

async def start_withdraw(update, context):
    if not private_only(update):
        return ConversationHandler.END

    row = get_user(update.effective_user.id)
    balance = float(row["saldo"]) if row else 0

    await update.message.reply_text(
        "💸 *SOLICITAR RETIRO*\n\n"
        f"Saldo disponible: *{money(balance)} USDT*\n"
        f"Mínimo: *{money(MIN_WITHDRAWAL)} USDT*\n"
        "Frecuencia: *1 retiro cada 7 días*\n\n"
        "Introduce el monto que deseas retirar.",
        parse_mode="Markdown"
    )

    context.user_data.clear()
    return WITHDRAW_AMOUNT


async def receive_withdraw_amount(update, context):
    if not private_only(update):
        return ConversationHandler.END

    try:
        amount = float(update.message.text.replace(",", ".").strip())
    except ValueError:
        await update.message.reply_text("⚠️ Introduce un monto válido.")
        return WITHDRAW_AMOUNT

    if amount < MIN_WITHDRAWAL:
        await update.message.reply_text(
            f"⚠️ El retiro mínimo es de *{money(MIN_WITHDRAWAL)} USDT*.",
            parse_mode="Markdown"
        )
        return WITHDRAW_AMOUNT

    can_withdraw, remaining = withdrawal_wait_info(update.effective_user.id)
    if not can_withdraw:
        await update.message.reply_text(
            "⏳ *RETIRO SEMANAL*\n\n"
            f"Ya realizaste una solicitud de retiro.\n"
            f"Podrás solicitar otro retiro en aproximadamente *{withdrawal_wait_text(remaining)}*.",
            parse_mode="Markdown"
        )
        return WITHDRAW_AMOUNT

    balance = user_balance(update.effective_user.id)

    if amount > balance:
        await update.message.reply_text(
            f"⚠️ Saldo insuficiente.\nDisponible: {money(balance)} USDT"
        )
        return WITHDRAW_AMOUNT

    context.user_data["withdraw_amount"] = amount

    await update.message.reply_text(
        "📍 Envía ahora tu dirección *USDT TRC20*.",
        parse_mode="Markdown"
    )
    return WITHDRAW_ADDRESS


async def receive_withdraw_address(update, context):
    if not private_only(update):
        return ConversationHandler.END

    address = update.message.text.strip()
    amount = float(context.user_data.get("withdraw_amount", 0))
    user_id = update.effective_user.id

    if amount < MIN_WITHDRAWAL:
        await update.message.reply_text(f"⚠️ El retiro mínimo es de *{money(MIN_WITHDRAWAL)} USDT*.", parse_mode="Markdown")
        return WITHDRAW_AMOUNT

    can_withdraw, remaining = withdrawal_wait_info(user_id)
    if not can_withdraw:
        await update.message.reply_text(
            "⏳ *RETIRO SEMANAL*\n\n"
            f"Ya realizaste una solicitud de retiro.\nPodrás solicitar otro en aproximadamente *{withdrawal_wait_text(remaining)}*.",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    if len(address) < 20:
        await update.message.reply_text(
            "⚠️ La dirección parece inválida. Envíala nuevamente."
        )
        return WITHDRAW_ADDRESS

    conn = db()

    row = conn.execute(
        "SELECT saldo FROM usuarios WHERE telegram_id = ?",
        (user_id,)
    ).fetchone()

    if not row or float(row["saldo"]) < amount:
        conn.close()
        await update.message.reply_text(
            "⚠️ El saldo ya no es suficiente para esta solicitud."
        )
        return ConversationHandler.END

    conn.execute("""
        UPDATE usuarios
        SET saldo = saldo - ?
        WHERE telegram_id = ?
    """, (amount, user_id))

    cur = conn.execute("""
        INSERT INTO retiros (
            telegram_id, monto, direccion, estado, fecha
        )
        VALUES (?, ?, ?, 'pendiente', ?)
    """, (
        user_id,
        amount,
        address,
        now_iso()
    ))

    withdrawal_id = cur.lastrowid
    conn.commit()
    conn.close()

    add_movement(
        user_id,
        "retiro_pendiente",
        amount,
        f"Retiro #{withdrawal_id} enviado para revisión"
    )

    await update.message.reply_text(
        "⏳ *RETIRO SOLICITADO*\n\n"
        f"ID: `{withdrawal_id}`\n"
        f"Monto: *{money(amount)} USDT*\n"
        f"Red: *TRC20*\n"
        f"Dirección: `{address}`\n\n"
        "El administrador revisará y procesará la solicitud.",
        parse_mode="Markdown"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✅ Aprobar",
                callback_data=f"wd_approve_{withdrawal_id}"
            ),
            InlineKeyboardButton(
                "❌ Rechazar",
                callback_data=f"wd_reject_{withdrawal_id}"
            )
        ]
    ])

    try:
        await context.bot.send_message(
            chat_id=ADMIN_TELEGRAM_ID,
            text=(
                "📤 *NUEVO RETIRO PENDIENTE*\n\n"
                f"ID: `{withdrawal_id}`\n"
                f"Usuario: `{user_id}`\n"
                f"Monto: *{money(amount)} USDT*\n"
                f"Dirección TRC20:\n`{address}`"
            ),
            parse_mode="Markdown",
            reply_markup=keyboard
        )
    except Exception as e:
        print(f"Error notificando retiro al admin: {e}")

    context.user_data.clear()
    return ConversationHandler.END


async def cancel_withdraw(update, context):
    context.user_data.clear()
    await update.message.reply_text("❌ Retiro cancelado.")
    return ConversationHandler.END


# =========================================================
# HISTORIAL
# =========================================================

async def show_history(query):
    user_id = query.from_user.id
    conn = db()
    rows = conn.execute("""
        SELECT tipo, monto, descripcion, fecha
        FROM movimientos
        WHERE telegram_id = ?
        ORDER BY id ASC
    """, (user_id,)).fetchall()
    conn.close()

    if not rows:
        texto = "📜 *HISTORIAL COMPLETO*\n\nTodavía no tienes movimientos."
    else:
        texto = "📜 *HISTORIAL COMPLETO*\n\n"
        for r in rows:
            try:
                fecha = datetime.fromisoformat(r["fecha"]).astimezone(timezone.utc).strftime("%d/%m/%Y %H:%M")
            except Exception:
                fecha = r["fecha"]
            texto += (
                f"🕒 {fecha} UTC\n"
                f"• *{r['tipo']}* — {money(r['monto'])} USDT\n"
                f"  {r['descripcion']}\n\n"
            )

    # Telegram tiene límite de 4096 caracteres; si hay mucho historial, enviamos por páginas.
    chunks = [texto[i:i+3800] for i in range(0, len(texto), 3800)]
    if not chunks:
        chunks = [texto]
    await query.edit_message_text(
        chunks[0], parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Atrás", callback_data="user_home"), InlineKeyboardButton("🏠 Menú Principal", callback_data="user_home")]
        ])
    )
    for extra in chunks[1:]:
        await query.message.reply_text(extra, parse_mode="Markdown")


# =========================================================
# CALLBACKS
# =========================================================

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.message is None:
        return

    # TODO EL PANEL PRIVADO
    if query.message.chat.type != ChatType.PRIVATE:
        await query.answer(
            "Este menú solo funciona en el chat privado con el bot.",
            show_alert=True
        )
        return

    user_id = query.from_user.id
    data = query.data or ""

    print(
        f"[CALLBACK] user_id={user_id} "
        f"chat_id={query.message.chat.id} data={data}"
    )

    # -----------------------------------------------------
    # ADMIN
    # -----------------------------------------------------

    if (data.startswith("admin_") or data.startswith("dep_approve_") or
            data.startswith("dep_reject_") or data.startswith("wd_approve_") or
            data.startswith("wd_reject_")):
        if not is_admin(user_id):
            await query.answer(
                "⛔ No autorizado.",
                show_alert=True
            )
            return

    if data == "admin_users":
        conn = db()
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM usuarios"
        ).fetchone()["c"]
        total = conn.execute(
            "SELECT COALESCE(SUM(total_depositado), 0) AS s FROM usuarios"
        ).fetchone()["s"]
        invested = conn.execute(
            "SELECT COALESCE(SUM(invertido), 0) AS s FROM usuarios"
        ).fetchone()["s"]
        conn.close()

        await query.edit_message_text(
            "👥 *USUARIOS*\n\n"
            f"Usuarios registrados: *{count}*\n"
            f"Total depositado: *{money(total)} USDT*\n"
            f"Capital actualmente invertido: *{money(invested)} USDT*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Panel", callback_data="admin_home")]
            ])
        )
        return

    if data == "admin_deposits":
        conn = db()
        rows = conn.execute("""
            SELECT *
            FROM depositos
            WHERE estado = 'pendiente'
            ORDER BY id DESC
            LIMIT 10
        """).fetchall()
        conn.close()

        if not rows:
            text = "📥 *DEPÓSITOS PENDIENTES*\n\nNo hay depósitos pendientes."
        else:
            text = "📥 *DEPÓSITOS PENDIENTES*\n\n"
            for r in rows:
                text += (
                    f"#{r['id']} — Usuario `{r['telegram_id']}`\n"
                    f"Monto: *{money(r['monto'])} USDT*\n"
                    f"TX: `{r['tx_hash']}`\n\n"
                )

        await query.edit_message_text(
            text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Panel", callback_data="admin_home")]
            ])
        )
        return

    if data == "admin_withdrawals":
        conn = db()
        rows = conn.execute("""
            SELECT *
            FROM retiros
            WHERE estado = 'pendiente'
            ORDER BY id DESC
            LIMIT 10
        """).fetchall()
        conn.close()

        if not rows:
            text = "📤 *RETIROS PENDIENTES*\n\nNo hay retiros pendientes."
        else:
            text = "📤 *RETIROS PENDIENTES*\n\n"
            for r in rows:
                text += (
                    f"#{r['id']} — Usuario `{r['telegram_id']}`\n"
                    f"Monto: *{money(r['monto'])} USDT*\n"
                    f"Dirección: `{r['direccion']}`\n\n"
                )

        await query.edit_message_text(
            text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Panel", callback_data="admin_home")]
            ])
        )
        return

    if data == "admin_investments":
        conn = db()
        active = conn.execute("""
            SELECT COUNT(*) AS c
            FROM inversiones
            WHERE estado = 'activa'
        """).fetchone()["c"]

        capital = conn.execute("""
            SELECT COALESCE(SUM(capital), 0) AS s
            FROM inversiones
            WHERE estado = 'activa'
        """).fetchone()["s"]

        profit = conn.execute("""
            SELECT COALESCE(SUM(ganancia_acumulada), 0) AS s
            FROM inversiones
        """).fetchone()["s"]

        conn.close()

        await query.edit_message_text(
            "📈 *INVERSIONES*\n\n"
            f"Activas: *{active}*\n"
            f"Capital activo: *{money(capital)} USDT*\n"
            f"Ganancias acumuladas: *{money(profit)} USDT*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Panel", callback_data="admin_home")]
            ])
        )
        return

    if data == "admin_status":
        conn = db()
        users = conn.execute(
            "SELECT COUNT(*) AS c FROM usuarios"
        ).fetchone()["c"]
        deposits = conn.execute(
            "SELECT COUNT(*) AS c FROM depositos WHERE estado='pendiente'"
        ).fetchone()["c"]
        withdrawals = conn.execute(
            "SELECT COUNT(*) AS c FROM retiros WHERE estado='pendiente'"
        ).fetchone()["c"]
        investments = conn.execute(
            "SELECT COUNT(*) AS c FROM inversiones WHERE estado='activa'"
        ).fetchone()["c"]
        conn.close()

        await query.edit_message_text(
            "📊 *ESTADO DEL SISTEMA*\n\n"
            "🟢 Bot: activo\n"
            f"👥 Usuarios: *{users}*\n"
            f"📥 Depósitos pendientes: *{deposits}*\n"
            f"📤 Retiros pendientes: *{withdrawals}*\n"
            f"📈 Inversiones activas: *{investments}*\n"
            f"💾 Base de datos: `{DB_FILE}`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Panel", callback_data="admin_home")]
            ])
        )
        return

    if data == "admin_profit":
        conn = db()
        total_deposits = conn.execute("SELECT COALESCE(SUM(monto),0) s FROM depositos WHERE estado='aprobado'").fetchone()["s"]
        active_capital = conn.execute("SELECT COALESCE(SUM(capital),0) s FROM inversiones WHERE estado='activa'").fetchone()["s"]
        pending_deposits = conn.execute("SELECT COALESCE(SUM(monto),0) s FROM depositos WHERE estado='pendiente'").fetchone()["s"]
        conn.close()
        daily_general = float(total_deposits) * DAILY_RATE
        await query.edit_message_text(
            "⚙️ *PROCESAR GANANCIAS*\n\n"
            f"💰 Total de depósitos aprobados: *{money(total_deposits)} USDT*\n"
            f"📈 Capital actualmente invertido: *{money(active_capital)} USDT*\n"
            f"⏳ Depósitos pendientes: *{money(pending_deposits)} USDT*\n"
            f"📊 Tasa diaria: *{DAILY_RATE * 100:.4g}%*\n"
            f"💵 Ganancia general diaria según depósitos aprobados: *{money(daily_general)} USDT*\n\n"
            "El botón de abajo ejecuta el cálculo de las inversiones activas desde su último cálculo.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("▶️ Procesar ahora", callback_data="admin_profit_execute")],
                [InlineKeyboardButton("⬅️ Panel", callback_data="admin_home")]
            ])
        )
        return

    if data == "admin_profit_execute":
        processed, total = process_profits()
        conn = db()
        total_deposits = conn.execute("SELECT COALESCE(SUM(monto),0) s FROM depositos WHERE estado='aprobado'").fetchone()["s"]
        conn.close()
        daily_general = float(total_deposits) * DAILY_RATE
        await query.edit_message_text(
            "✅ *GANANCIAS PROCESADAS*\n\n"
            f"💰 Capital total depositado aprobado: *{money(total_deposits)} USDT*\n"
            f"📊 Tasa diaria: *{DAILY_RATE * 100:.4g}%*\n"
            f"💵 Referencia de ganancia diaria general: *{money(daily_general)} USDT*\n"
            f"📈 Inversiones procesadas: *{processed}*\n"
            f"💵 Ganancia acreditada en esta ejecución: *{money(total)} USDT*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Panel", callback_data="admin_home")]])
        )
        return

    if data == "admin_backup":
        await query.edit_message_text("💾 *PREPARANDO RESPALDO EXCEL...*", parse_mode="Markdown")
        try:
            await send_excel_backup(context.bot, "Respaldo solicitado por el administrador")
            await send_admin_menu(ADMIN_TELEGRAM_ID, context, "✅ *RESPALDO CREADO Y ENVIADO*\n\nPanel administrativo:")
        except Exception as e:
            await query.edit_message_text(
                f"❌ *ERROR AL CREAR RESPALDO*\n\n`{str(e)}`",
                parse_mode="Markdown",
                reply_markup=back_inline()
            )
        return
    if data == "admin_user_menu":
        await send_user_menu(
            ADMIN_TELEGRAM_ID,
            context,
            "👤 *MENÚ DE USUARIO*\n\n"
            "Estás viendo el menú normal como administrador."
        )
        return

    if data == "admin_home":
        await send_admin_menu(
            ADMIN_TELEGRAM_ID,
            context
        )
        return

    # -----------------------------------------------------
    # APROBAR/RECHAZAR DEPÓSITO
    # -----------------------------------------------------

    if data.startswith("dep_approve_"):
        deposit_id = int(data.rsplit("_", 1)[1])

        conn = db()
        row = conn.execute(
            "SELECT * FROM depositos WHERE id = ?",
            (deposit_id,)
        ).fetchone()

        if not row or row["estado"] != "pendiente":
            conn.close()
            await query.answer(
                "Este depósito ya fue procesado.",
                show_alert=True
            )
            return

        amount = float(row["monto"])
        uid = row["telegram_id"]

        conn.execute("""
            UPDATE depositos
            SET estado='aprobado', fecha_revision=?, revisado_por=?
            WHERE id=?
        """, (now_iso(), user_id, deposit_id))

        conn.execute("""
            UPDATE usuarios
            SET saldo = saldo + ?,
                total_depositado = total_depositado + ?
            WHERE telegram_id = ?
        """, (amount, amount, uid))

        conn.commit()
        conn.close()

        add_movement(
            uid,
            "deposito",
            amount,
            f"Depósito #{deposit_id} aprobado"
        )

        await query.edit_message_text(
            f"✅ *DEPÓSITO #{deposit_id} APROBADO*\n\n"
            f"Monto acreditado: *{money(amount)} USDT*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    "⬅️ Panel",
                    callback_data="admin_home"
                )]
            ])
        )

        try:
            await context.bot.send_message(
                chat_id=uid,
                text=(
                    "✅ *DEPÓSITO APROBADO*\n\n"
                    f"Tu depósito de *{money(amount)} USDT* "
                    "fue aprobado y acreditado a tu saldo."
                ),
                parse_mode="Markdown"
            )
        except Exception as e:
            print(f"Error notificando aprobación de depósito: {e}")

        return

    if data.startswith("dep_reject_"):
        deposit_id = int(data.rsplit("_", 1)[1])

        conn = db()
        row = conn.execute(
            "SELECT * FROM depositos WHERE id=?",
            (deposit_id,)
        ).fetchone()

        if not row or row["estado"] != "pendiente":
            conn.close()
            await query.answer(
                "Este depósito ya fue procesado.",
                show_alert=True
            )
            return

        conn.execute("""
            UPDATE depositos
            SET estado='rechazado', fecha_revision=?, revisado_por=?
            WHERE id=?
        """, (now_iso(), user_id, deposit_id))
        conn.commit()
        conn.close()

        await query.edit_message_text(
            f"❌ *DEPÓSITO #{deposit_id} RECHAZADO*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    "⬅️ Panel",
                    callback_data="admin_home"
                )]
            ])
        )

        try:
            await context.bot.send_message(
                chat_id=row["telegram_id"],
                text=(
                    "❌ *DEPÓSITO RECHAZADO*\n\n"
                    f"El depósito #{deposit_id} fue rechazado. "
                    "Contacta al administrador si necesitas revisar el caso."
                ),
                parse_mode="Markdown"
            )
        except Exception as e:
            print(f"Error notificando rechazo: {e}")

        return

    # -----------------------------------------------------
    # APROBAR/RECHAZAR RETIRO
    # -----------------------------------------------------

    if data.startswith("wd_approve_"):
        withdrawal_id = int(data.rsplit("_", 1)[1])

        conn = db()
        row = conn.execute(
            "SELECT * FROM retiros WHERE id=?",
            (withdrawal_id,)
        ).fetchone()

        if not row or row["estado"] != "pendiente":
            conn.close()
            await query.answer(
                "Este retiro ya fue procesado.",
                show_alert=True
            )
            return

        conn.execute("""
            UPDATE retiros
            SET estado='aprobado', fecha_revision=?, revisado_por=?
            WHERE id=?
        """, (now_iso(), user_id, withdrawal_id))

        conn.execute("""
            UPDATE usuarios
            SET total_retirado = total_retirado + ?
            WHERE telegram_id=?
        """, (row["monto"], row["telegram_id"]))

        conn.commit()
        conn.close()

        add_movement(
            row["telegram_id"],
            "retiro",
            row["monto"],
            f"Retiro #{withdrawal_id} aprobado"
        )

        await query.edit_message_text(
            f"✅ *RETIRO #{withdrawal_id} APROBADO*\n\n"
            f"Monto: *{money(row['monto'])} USDT*\n\n"
            "El administrador debe realizar el envío a la dirección indicada.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    "⬅️ Panel",
                    callback_data="admin_home"
                )]
            ])
        )

        try:
            await context.bot.send_message(
                chat_id=row["telegram_id"],
                text=(
                    "✅ *RETIRO APROBADO*\n\n"
                    f"Tu solicitud #{withdrawal_id} por "
                    f"*{money(row['monto'])} USDT* fue aprobada.\n\n"
                    "El envío será procesado por el administrador."
                ),
                parse_mode="Markdown"
            )
        except Exception as e:
            print(f"Error notificando retiro aprobado: {e}")

        return

    if data.startswith("wd_reject_"):
        withdrawal_id = int(data.rsplit("_", 1)[1])

        conn = db()
        row = conn.execute(
            "SELECT * FROM retiros WHERE id=?",
            (withdrawal_id,)
        ).fetchone()

        if not row or row["estado"] != "pendiente":
            conn.close()
            await query.answer(
                "Este retiro ya fue procesado.",
                show_alert=True
            )
            return

        conn.execute("""
            UPDATE retiros
            SET estado='rechazado', fecha_revision=?, revisado_por=?
            WHERE id=?
        """, (now_iso(), user_id, withdrawal_id))

        conn.execute("""
            UPDATE usuarios
            SET saldo = saldo + ?
            WHERE telegram_id=?
        """, (row["monto"], row["telegram_id"]))

        conn.commit()
        conn.close()

        add_movement(
            row["telegram_id"],
            "devolucion_retiro",
            row["monto"],
            f"Retiro #{withdrawal_id} rechazado; saldo devuelto"
        )

        await query.edit_message_text(
            f"❌ *RETIRO #{withdrawal_id} RECHAZADO*\n\n"
            "El saldo fue devuelto al usuario.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    "⬅️ Panel",
                    callback_data="admin_home"
                )]
            ])
        )

        try:
            await context.bot.send_message(
                chat_id=row["telegram_id"],
                text=(
                    "❌ *RETIRO RECHAZADO*\n\n"
                    f"El retiro #{withdrawal_id} fue rechazado y "
                    f"{money(row['monto'])} USDT fueron devueltos a tu saldo."
                ),
                parse_mode="Markdown"
            )
        except Exception as e:
            print(f"Error notificando retiro rechazado: {e}")

        return

    # -----------------------------------------------------
    # USUARIO
    # -----------------------------------------------------

    if data == "user_home":
        await send_user_menu(
            user_id,
            context,
            "🏦 *MENÚ PRINCIPAL*\n\nSelecciona una opción:"
        )
        return

    if data == "user_account":
        await show_account(query)
        return

    if data == "user_invest":
        await show_investments(query)
        return

    if data == "user_plans":
        await show_plans(query)
        return

    if data.startswith("plan_"):
        try:
            amount = float(data.split("_", 1)[1])
        except ValueError:
            await query.answer("Plan inválido.", show_alert=True)
            return
        await select_plan(query, amount)
        return

    if data.startswith("confirm_plan_"):
        try:
            amount = float(data.split("_", 2)[2])
        except ValueError:
            await query.answer("Plan inválido.", show_alert=True)
            return
        await confirm_plan_investment(query, amount)
        return

    if data.startswith("deposit_plan_"):
        try:
            amount = float(data.split("_", 2)[2])
        except ValueError:
            await query.answer("Plan inválido.", show_alert=True)
            return
        await start_plan_deposit(query, context, amount)
        return

    if data == "user_new_investment":
        await create_investment(query)
        return

    if data == "user_confirm_invest":
        await confirm_investment(query)
        return

    if data == "user_referrals":
        await show_referrals(query, context)
        return

    if data == "user_history":
        await show_history(query)
        return

    if data == "user_info":
        await show_info(query)
        return

    if data == "user_deposit":
        await query.edit_message_text(
            "💰 *NUEVO DEPÓSITO*\n\n"
            f"Wallet USDT TRC20:\n`{USDT_TRC20_ADDRESS or 'NO CONFIGURADA'}`\n\n"
            "Puedes elegir un plan para que el monto quede fijado automáticamente.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💎 Ver Planes de Inversión", callback_data="user_plans")],
                [InlineKeyboardButton("⬅️ Atrás", callback_data="user_home"), InlineKeyboardButton("🏠 Menú Principal", callback_data="user_home")]
            ])
        )
        context.user_data.clear()
        return

    if data == "user_withdraw":
        await query.edit_message_text(
            "💸 *RETIRO*\n\n"
            "Escribe ahora el monto que deseas retirar.",
            parse_mode="Markdown"
        )
        context.user_data.clear()
        context.user_data["manual_flow"] = "withdraw_amount"
        return


# =========================================================
# TEXTO PRIVADO
# =========================================================

async def private_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not private_only(update):
        return
    text = (update.message.text or "").strip()
    if not text:
        return

    ensure_user(update.effective_user)

    admin_actions = {
        "👥 Usuarios": "admin_users", "📥 Depósitos": "admin_deposits",
        "📤 Retiros": "admin_withdrawals", "📈 Inversiones": "admin_investments",
        "💾 Crear respaldo": "admin_backup", "📊 Estado": "admin_status",
        "⚙️ Procesar ganancias": "admin_profit", "👤 Menú usuario": "admin_user_menu",
    }
    user_actions = {
        "👤 Mi cuenta": "user_account", "📈 Inversiones": "user_invest",
        "🤝 Referidos": "user_referrals", "📜 Historial": "user_history",
        "ℹ️ Información": "user_info", "💎 Planes de Inversión": "user_plans",
    }

    # El teclado inferior funciona como panel fijo.
    if is_admin(update.effective_user.id) and text in admin_actions:
        await handle_text_panel_action(update, context, admin_actions[text])
        return
    if text in user_actions:
        await handle_text_panel_action(update, context, user_actions[text])
        return
    if text == "💰 Depositar":
        context.user_data.clear()
        await update.message.reply_text(
            "💰 *NUEVO DEPÓSITO*\n\n"
            f"Wallet USDT TRC20:\n`{USDT_TRC20_ADDRESS or 'NO CONFIGURADA'}`\n\n"
            "Selecciona un plan y el monto del depósito quedará fijado automáticamente.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💎 Ver Planes de Inversión", callback_data="user_plans")],
                [InlineKeyboardButton("⬅️ Atrás", callback_data="user_home"), InlineKeyboardButton("🏠 Menú Principal", callback_data="user_home")]
            ])
        )
        return
    if text == "💸 Retirar":
        can_withdraw, remaining = withdrawal_wait_info(update.effective_user.id)
        if not can_withdraw:
            await update.message.reply_text(
                "⏳ *RETIRO SEMANAL*\n\n"
                f"Ya realizaste una solicitud de retiro.\nPodrás solicitar otro en aproximadamente *{withdrawal_wait_text(remaining)}*.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menú Principal", callback_data="user_home")]])
            )
            return
        context.user_data.clear()
        context.user_data["manual_flow"] = "withdraw_amount"
        row = get_user(update.effective_user.id)
        balance = float(row["saldo"]) if row else 0
        await update.message.reply_text(
            "💸 *SOLICITAR RETIRO*\n\n"
            f"Saldo disponible: *{money(balance)} USDT*\n"
            f"Mínimo: *{money(MIN_WITHDRAWAL)} USDT*\n"
            "Frecuencia: *1 retiro cada 7 días*\n\n"
            "Escribe ahora el monto que deseas retirar.",
            parse_mode="Markdown"
        )
        return

    flow = context.user_data.get("manual_flow")
    if flow == "deposit_tx_fixed":
        if len(text) < 5:
            await update.message.reply_text("⚠️ El TXID parece demasiado corto. Envíalo nuevamente.")
            return
        context.user_data["deposit_tx"] = text
        context.user_data["manual_flow"] = "deposit_photo"
        await update.message.reply_text(
            "📸 Ahora envía una *captura del comprobante*.\n\nSi no puedes enviar captura, escribe /skip.",
            parse_mode="Markdown"
        )
        return
    if flow == "deposit_amount":
        context.user_data["deposit_amount"] = None
        try:
            amount = float(text.replace(",", "."))
        except ValueError:
            await update.message.reply_text("⚠️ Introduce solamente un número.")
            return
        if amount <= 0:
            await update.message.reply_text("⚠️ El monto debe ser mayor que 0.")
            return
        context.user_data["deposit_amount"] = amount
        context.user_data["manual_flow"] = "deposit_tx"
        await update.message.reply_text("🔗 Ahora envía el *hash de la transacción (TXID)*.", parse_mode="Markdown")
        return
    if flow == "deposit_tx":
        if len(text) < 5:
            await update.message.reply_text("⚠️ El TXID parece demasiado corto. Envíalo nuevamente.")
            return
        context.user_data["deposit_tx"] = text
        context.user_data["manual_flow"] = "deposit_photo"
        await update.message.reply_text(
            "📸 Ahora envía una *captura del comprobante*.\n\nSi no puedes enviar captura, escribe /skip.",
            parse_mode="Markdown"
        )
        # La captura será atendida por el ConversationHandler cuando se use /depositar;
        # para el flujo del teclado aceptamos también la siguiente foto en photo_text_handler.
        return
    if flow == "withdraw_amount":
        try:
            amount = float(text.replace(",", "."))
        except ValueError:
            await update.message.reply_text("⚠️ Introduce un monto válido.")
            return
        if amount < MIN_WITHDRAWAL:
            await update.message.reply_text(
                f"⚠️ El retiro mínimo es de *{money(MIN_WITHDRAWAL)} USDT*.",
                parse_mode="Markdown"
            )
            return
        can_withdraw, remaining = withdrawal_wait_info(update.effective_user.id)
        if not can_withdraw:
            await update.message.reply_text(
                "⏳ *RETIRO SEMANAL*\n\n"
                f"Ya realizaste una solicitud de retiro.\n"
                f"Podrás solicitar otro retiro en aproximadamente *{withdrawal_wait_text(remaining)}*.",
                parse_mode="Markdown"
            )
            return
        balance = user_balance(update.effective_user.id)
        if amount > balance:
            await update.message.reply_text(f"⚠️ Saldo insuficiente.\nDisponible: {money(balance)} USDT")
            return
        context.user_data["withdraw_amount"] = amount
        context.user_data["manual_flow"] = "withdraw_address"
        await update.message.reply_text("📍 Envía ahora tu dirección *USDT TRC20*.", parse_mode="Markdown")
        return
    if flow == "withdraw_address":
        context.user_data["manual_flow"] = None
        # Reutilizar la lógica existente de retiro.
        context.user_data["withdraw_amount"] = context.user_data.get("withdraw_amount", 0)
        await finish_withdraw_manual(update, context, text)
        return


async def handle_text_panel_action(update, context, action):
    # Construye un callback artificial para reutilizar las funciones existentes sin duplicarlas.
    class FakeQuery:
        def __init__(self, message, user):
            self.message = message
            self.from_user = user
        async def edit_message_text(self, *args, **kwargs):
            return await update.message.reply_text(*args, **kwargs)
        async def answer(self, *args, **kwargs):
            return None
    fake = FakeQuery(update.message, update.effective_user)
    if action == "admin_users":
        conn=db(); count=conn.execute("SELECT COUNT(*) c FROM usuarios").fetchone()["c"]; total=conn.execute("SELECT COALESCE(SUM(total_depositado),0) s FROM usuarios").fetchone()["s"]; invested=conn.execute("SELECT COALESCE(SUM(invertido),0) s FROM usuarios").fetchone()["s"]; conn.close()
        await update.message.reply_text(f"👥 *USUARIOS*\n\nUsuarios registrados: *{count}*\nTotal depositado: *{money(total)} USDT*\nCapital actualmente invertido: *{money(invested)} USDT*", parse_mode="Markdown")
    elif action == "admin_deposits": await update.message.reply_text("📥 Usa el panel para revisar los depósitos pendientes.")
    elif action == "admin_withdrawals": await update.message.reply_text("📤 Usa el panel para revisar los retiros pendientes.")
    elif action == "admin_investments": await update.message.reply_text("📈 *INVERSIONES*\n\nConsulta el resumen actualizado desde el panel.", parse_mode="Markdown")
    elif action == "admin_backup": await send_excel_backup(context.bot, "Respaldo solicitado por el administrador")
    elif action == "admin_status": await update.message.reply_text("📊 *ESTADO*\n\nBot activo.", parse_mode="Markdown")
    elif action == "admin_profit": await update.message.reply_text("⚙️ Pulsa el botón inline *Procesar ahora* para ejecutar las ganancias.", parse_mode="Markdown")
    elif action == "admin_user_menu": await send_user_menu(update.effective_chat.id, context, "👤 *MENÚ DE USUARIO*")
    elif action == "user_account": await show_account(fake)
    elif action == "user_invest": await show_investments(fake)
    elif action == "user_plans": await show_plans(fake)
    elif action == "user_referrals": await show_referrals(fake, context)
    elif action == "user_history": await show_history(fake)
    elif action == "user_info": await show_info(fake)


async def finish_withdraw_manual(update, context, address):
    user_id = update.effective_user.id
    amount = float(context.user_data.get("withdraw_amount", 0))
    if amount < MIN_WITHDRAWAL:
        context.user_data["manual_flow"] = "withdraw_amount"
        await update.message.reply_text(f"⚠️ El retiro mínimo es de *{money(MIN_WITHDRAWAL)} USDT*.", parse_mode="Markdown")
        return
    can_withdraw, remaining = withdrawal_wait_info(user_id)
    if not can_withdraw:
        context.user_data.clear()
        await update.message.reply_text(
            "⏳ *RETIRO SEMANAL*\n\n"
            f"Ya realizaste una solicitud de retiro.\nPodrás solicitar otro en aproximadamente *{withdrawal_wait_text(remaining)}*.",
            parse_mode="Markdown"
        )
        return
    if len(address) < 20:
        context.user_data["manual_flow"] = "withdraw_address"
        await update.message.reply_text("⚠️ La dirección parece inválida. Envíala nuevamente.")
        return
    conn = db()
    row = conn.execute("SELECT saldo FROM usuarios WHERE telegram_id=?", (user_id,)).fetchone()
    if not row or float(row["saldo"]) < amount:
        conn.close()
        await update.message.reply_text("⚠️ El saldo ya no es suficiente para esta solicitud.")
        return
    conn.execute("UPDATE usuarios SET saldo=saldo-? WHERE telegram_id=?", (amount,user_id))
    cur = conn.execute("INSERT INTO retiros (telegram_id,monto,direccion,estado,fecha) VALUES (?,?,?,'pendiente',?)", (user_id,amount,address,now_iso()))
    wid = cur.lastrowid
    conn.commit()
    conn.close()
    add_movement(user_id,"retiro_pendiente",amount,f"Retiro #{wid} enviado para revisión")
    await update.message.reply_text(
        "⏳ *RETIRO SOLICITADO*\n\n"
        f"ID: `{wid}`\nMonto: *{money(amount)} USDT*\nRed: *TRC20*\nDirección: `{address}`\n\n"
        "El administrador revisará y procesará la solicitud.", parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menú Principal", callback_data="user_home")]])
    )
    kb=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Aprobar",callback_data=f"wd_approve_{wid}"),InlineKeyboardButton("❌ Rechazar",callback_data=f"wd_reject_{wid}")]])
    try:
        await context.bot.send_message(chat_id=ADMIN_TELEGRAM_ID,text=("📤 *NUEVO RETIRO PENDIENTE*\n\n" f"ID: `{wid}`\nUsuario: `{user_id}`\nMonto: *{money(amount)} USDT*\nDirección TRC20:\n`{address}`"),parse_mode="Markdown",reply_markup=kb)
    except Exception as e:
        print(f"Error notificando retiro: {e}")
    context.user_data.clear()

# =========================================================
# RESPALDO EXCEL AUTOMÁTICO
# =========================================================

def create_excel_backup():
    conn = db()
    wb = Workbook()
    wb.remove(wb.active)
    tables = [
        "usuarios", "depositos", "retiros", "inversiones", "movimientos", "referidos"
    ]
    for table in tables:
        ws = wb.create_sheet(table[:31])
        rows = conn.execute(f"SELECT * FROM {table}").fetchall()
        columns = [d[1] for d in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        ws.append(columns)
        for row in rows:
            ws.append([row[col] for col in columns])
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
        for col_cells in ws.columns:
            max_len = max(len(str(c.value or "")) for c in col_cells[:200])
            ws.column_dimensions[col_cells[0].column_letter].width = min(max(max_len + 2, 12), 40)

    ws = wb.create_sheet("resumen")
    approved = conn.execute("SELECT COALESCE(SUM(monto),0) s FROM depositos WHERE estado='aprobado'").fetchone()["s"]
    active = conn.execute("SELECT COALESCE(SUM(capital),0) s FROM inversiones WHERE estado='activa'").fetchone()["s"]
    daily = float(approved) * DAILY_RATE
    ws.append(["Indicador", "Valor"])
    ws.append(["Fecha UTC", now_iso()])
    ws.append(["Tasa diaria", DAILY_RATE])
    ws.append(["Total depósitos aprobados (USDT)", float(approved)])
    ws.append(["Capital activo invertido (USDT)", float(active)])
    ws.append(["Ganancia diaria sobre depósitos aprobados (USDT)", daily])
    ws.freeze_panes = "A2"
    ws.column_dimensions["A"].width = 48
    ws.column_dimensions["B"].width = 24
    conn.close()

    output = BytesIO()
    wb.save(output)
    output.seek(0)
    return output


async def send_excel_backup(bot, reason="Respaldo automático"):
    filename = f"respaldo_inversion_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.xlsx"
    output = create_excel_backup()
    await bot.send_document(
        chat_id=ADMIN_TELEGRAM_ID,
        document=InputFile(output, filename=filename),
        caption=f"💾 {reason}\n📊 Respaldo completo en Excel (.xlsx)."
    )


async def automatic_backup_loop(application):
    while True:
        try:
            tz = ZoneInfo(BACKUP_TIMEZONE)
        except Exception:
            print(f"⚠️ Zona horaria inválida: {BACKUP_TIMEZONE}. Se usará UTC.")
            tz = timezone.utc
        now = datetime.now(tz)
        try:
            hour, minute = [int(x) for x in BACKUP_TIME.split(":", 1)]
        except Exception:
            hour, minute = 6, 0
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        wait_seconds = max(1, (target - now).total_seconds())
        print(f"💾 Próximo respaldo automático: {target.isoformat()}")
        await asyncio.sleep(wait_seconds)
        try:
            await send_excel_backup(application.bot, "Respaldo automático diario de las 06:00")
            await send_admin_menu(ADMIN_TELEGRAM_ID, application, "✅ *RESPALDO AUTOMÁTICO ENVIADO*\\n\\nPanel administrativo:")
        except Exception as e:
            print(f"❌ Error en respaldo automático: {e}")


# =========================================================
# COMANDOS ADMIN
# =========================================================

async def manual_deposit_photo(update, context):
    if not private_only(update):
        return
    if context.user_data.get("manual_flow") != "deposit_photo":
        return
    photo = update.message.photo[-1]
    context.user_data["deposit_photo"] = photo.file_id
    await finish_deposit(update, context)


async def skip_manual_deposit_photo(update, context):
    if context.user_data.get("deposit_amount") and context.user_data.get("deposit_tx") and context.user_data.get("manual_flow") == "deposit_photo":
        context.user_data["deposit_photo"] = ""
        await finish_deposit(update, context)


async def backup_command(update, context):
    if not private_only(update):
        return

    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ No autorizado.")
        return

    os.makedirs(BACKUP_DIR, exist_ok=True)
    filename = f"database_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
    path = os.path.join(BACKUP_DIR, filename)

    try:
        conn = db()
        backup_conn = sqlite3.connect(path)
        conn.backup(backup_conn)
        backup_conn.close()
        conn.close()

        with open(path, "rb") as f:
            await context.bot.send_document(
                chat_id=ADMIN_TELEGRAM_ID,
                document=InputFile(f, filename=filename),
                caption="💾 Respaldo de la base de datos."
            )

        await send_admin_menu(
            ADMIN_TELEGRAM_ID,
            context,
            "✅ *RESPALDO CREADO*\n\nPanel administrativo:"
        )
    except Exception as e:
        await update.message.reply_text(
            f"❌ Error creando respaldo:\n`{e}`",
            parse_mode="Markdown"
        )


async def status_command(update, context):
    if not private_only(update):
        return

    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ No autorizado.")
        return

    conn = db()
    users = conn.execute("SELECT COUNT(*) AS c FROM usuarios").fetchone()["c"]
    deposits = conn.execute(
        "SELECT COUNT(*) AS c FROM depositos WHERE estado='pendiente'"
    ).fetchone()["c"]
    withdrawals = conn.execute(
        "SELECT COUNT(*) AS c FROM retiros WHERE estado='pendiente'"
    ).fetchone()["c"]
    conn.close()

    await update.message.reply_text(
        "📊 *ESTADO*\n\n"
        f"Usuarios: *{users}*\n"
        f"Depósitos pendientes: *{deposits}*\n"
        f"Retiros pendientes: *{withdrawals}*",
        parse_mode="Markdown"
    )


# =========================================================
# ERRORES
# =========================================================

async def error_handler(update, context):
    print(f"❌ Error: {context.error}")


# =========================================================
# MAIN
# =========================================================

async def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError(
            "Falta la variable de entorno TELEGRAM_TOKEN."
        )

    if ADMIN_TELEGRAM_ID == 0:
        raise RuntimeError(
            "Falta la variable de entorno ADMIN_TELEGRAM_ID."
        )

    init_db()
    await start_web_server()

    app = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .build()
    )

    # Obtener username del bot para construir links de referido.
    try:
        me = await app.bot.get_me()
        app.bot_data["bot_username"] = me.username or ""
        print(f"🤖 Bot: @{me.username}")
    except Exception as e:
        print(f"⚠️ No se pudo obtener username del bot: {e}")
        app.bot_data["bot_username"] = ""

    # -------------------------------
    # /start
    # -------------------------------
    app.add_handler(
        CommandHandler("start", start_command, filters=filters.ChatType.PRIVATE)
    )

    # -------------------------------
    # Admin
    # -------------------------------
    app.add_handler(
        CommandHandler("admin", admin_command, filters=filters.ChatType.PRIVATE)
    )
    app.add_handler(
        CommandHandler("backup", backup_command, filters=filters.ChatType.PRIVATE)
    )
    app.add_handler(
        CommandHandler("status", status_command, filters=filters.ChatType.PRIVATE)
    )

    # -------------------------------
    # Depósitos
    # -------------------------------
    deposit_handler = ConversationHandler(
        entry_points=[
            CommandHandler(
                "depositar",
                start_deposit,
                filters=filters.ChatType.PRIVATE
            )
        ],
        states={
            DEP_AMOUNT: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
                    receive_deposit_amount
                )
            ],
            DEP_TX: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
                    receive_deposit_tx
                )
            ],
            DEP_PHOTO: [
                MessageHandler(
                    filters.PHOTO & filters.ChatType.PRIVATE,
                    receive_deposit_photo
                ),
                CommandHandler(
                    "skip",
                    skip_deposit_photo,
                    filters=filters.ChatType.PRIVATE
                )
            ],
        },
        fallbacks=[
            CommandHandler(
                "cancelar",
                cancel_deposit,
                filters=filters.ChatType.PRIVATE
            )
        ],
        allow_reentry=True,
    )
    app.add_handler(deposit_handler)

    # -------------------------------
    # Retiros
    # -------------------------------
    withdraw_handler = ConversationHandler(
        entry_points=[
            CommandHandler(
                "retirar",
                start_withdraw,
                filters=filters.ChatType.PRIVATE
            )
        ],
        states={
            WITHDRAW_AMOUNT: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
                    receive_withdraw_amount
                )
            ],
            WITHDRAW_ADDRESS: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
                    receive_withdraw_address
                )
            ],
        },
        fallbacks=[
            CommandHandler(
                "cancelar",
                cancel_withdraw,
                filters=filters.ChatType.PRIVATE
            )
        ],
        allow_reentry=True,
    )
    app.add_handler(withdraw_handler)

    app.add_handler(CommandHandler("skip", skip_manual_deposit_photo, filters=filters.ChatType.PRIVATE))
    app.add_handler(MessageHandler(filters.PHOTO & filters.ChatType.PRIVATE, manual_deposit_photo))

    # -------------------------------
    # Botones
    # -------------------------------
    app.add_handler(
        CallbackQueryHandler(callback_handler)
    )

    # -------------------------------
    # Texto normal
    # -------------------------------
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
            private_text
        )
    )

    app.add_error_handler(error_handler)

    print("==============================================")
    print("🤖 BOT DE INVERSIÓN INICIADO")
    print(f"👑 ADMIN_TELEGRAM_ID = {ADMIN_TELEGRAM_ID}")
    print(f"💾 DB = {DB_FILE}")
    print(f"💰 Wallet TRC20 configurada = {bool(USDT_TRC20_ADDRESS)}")
    print("🔒 Funciones privadas: SOLO CHAT PRIVADO")
    print("==============================================")

    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    backup_task = asyncio.create_task(automatic_backup_loop(app))
    try:
        await asyncio.Event().wait()
    finally:
        backup_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
