import re
import asyncio
import logging
import sqlite3
import requests
from pathlib import Path

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)

# ================= НАСТРОЙКИ =================

BOT_TOKEN = "8273670933:AAHxaLl92JcNm9nfDd2mOlMA8DEMLBiCQpo"
POLL_INTERVAL_SEC = 2

DATA_API = "https://data-api.polymarket.com/activity"
DB_PATH = Path(__file__).resolve().with_name("watch.db")

ADDR_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("pm-bot")

# ====== состояния ввода (через user_data) ======
WAITING_ADDR = "waiting_addr"
WAITING_MIN = "waiting_min"
PENDING_MIN_ADDR = "pending_min_addr"

# ================= БАЗА =================

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS watches (
            chat_id INTEGER NOT NULL,
            address TEXT NOT NULL,
            last_seen_ts INTEGER NOT NULL DEFAULT 0,
            min_usdc REAL NOT NULL DEFAULT 0,
            PRIMARY KEY (chat_id, address)
        )
    """)
    return conn


def normalize(addr: str) -> str:
    return addr.lower().strip()


def parse_amount(s: str) -> float:
    s = s.strip().replace("_", "").replace(",", "")
    return float(s)


def trade_usdc(t: dict) -> float:
    try:
        return float(t.get("usdcSize") or 0)
    except Exception:
        return 0.0

# ================= POLYMARKET =================

def fetch_latest_trades(address: str, limit: int = 30):
    r = requests.get(DATA_API, params={
        "user": address,
        "type": "TRADE",
        "limit": limit,
        "offset": 0,
        "sortBy": "TIMESTAMP",
        "sortDirection": "DESC",
    }, timeout=15)

    if r.status_code == 429:
        retry_after = int(r.headers.get("Retry-After", "2"))
        raise RuntimeError(f"RATE_LIMIT:{retry_after}")

    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else []


def polymarket_url(t: dict):
    e = t.get("eventSlug")
    m = t.get("slug")
    if e and m:
        return f"https://polymarket.com/event/{e}/{m}"
    if e:
        return f"https://polymarket.com/event/{e}"
    return None

# ================= ФОРМАТ =================

def format_trade(t: dict) -> str:
    title = t.get("title") or "(без названия)"
    side = t.get("side") or "TRADE"
    outcome = t.get("outcome") or "-"
    price = t.get("price")
    usdc = t.get("usdcSize")
    tx = t.get("transactionHash")

    lines = [
        "🧾 *Сделка*",
        f"📌 *Событие:* {title}",
        f"🎯 *Outcome:* {outcome}",
        f"🧭 *Side:* {side}",
    ]

    if usdc is not None:
        try:
            lines.append(f"💵 *Сумма:* {round(float(usdc), 2)} USDC")
        except Exception:
            pass

    if price is not None:
        lines.append(f"🏷 *Цена:* {price}")

    if tx:
        lines.append(f"🔗 *Tx:* `{tx}`")

    url = polymarket_url(t)
    if url:
        lines.append(f"🌐 [Открыть событие]({url})")

    return "\n".join(lines)

# ================= UI (КНОПКИ МЕНЮ) =================

def main_menu_kb():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("➕ Watch"), KeyboardButton("📋 List")],
        ],
        resize_keyboard=True
    )

# ================= КОМАНДЫ =================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Панель управления ботом 👇\n"
        "➕ Watch — добавить адрес\n"
        "📋 List — список адресов\n\n"
        "Команды тоже работают:\n"
        "/watch 0x...\n"
        "/unwatch 0x...\n"
        "/min 0x... 10000\n"
        "/list",
        reply_markup=main_menu_kb()
    )

# ---- командный watch/unwatch/min/list (на всякий) ----

async def cmd_watch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("Пример: /watch 0x1234...")

    addr = normalize(context.args[0])
    if not ADDR_RE.match(addr):
        return await update.message.reply_text("❌ Неверный адрес.")

    chat_id = update.effective_chat.id

    last_seen_ts = 0
    try:
        trades = fetch_latest_trades(addr, limit=1)
        if trades:
            last_seen_ts = int(trades[0].get("timestamp") or 0)
    except Exception:
        pass

    conn = db()
    with conn:
        cur = conn.execute(
            "SELECT min_usdc FROM watches WHERE chat_id=? AND address=?",
            (chat_id, addr)
        )
        row = cur.fetchone()
        old_min = float(row[0]) if row else 0.0

        conn.execute(
            "INSERT OR REPLACE INTO watches(chat_id, address, last_seen_ts, min_usdc) VALUES(?,?,?,?)",
            (chat_id, addr, last_seen_ts, old_min)
        )
    conn.close()

    await update.message.reply_text(f"✅ Добавил {addr}", reply_markup=main_menu_kb())


async def cmd_unwatch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("Пример: /unwatch 0x1234...")

    addr = normalize(context.args[0])
    chat_id = update.effective_chat.id

    conn = db()
    with conn:
        cur = conn.execute(
            "DELETE FROM watches WHERE chat_id=? AND address=?",
            (chat_id, addr)
        )
        deleted = cur.rowcount
    conn.close()

    await update.message.reply_text(
        f"🛑 Удалил {addr}" if deleted else "Адрес не найден.",
        reply_markup=main_menu_kb()
    )


async def cmd_min(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        return await update.message.reply_text("Пример: /min 0x1234... 10000")

    addr = normalize(context.args[0])
    if not ADDR_RE.match(addr):
        return await update.message.reply_text("❌ Неверный адрес.")

    try:
        val = parse_amount(context.args[1])
    except Exception:
        return await update.message.reply_text("❌ Сумма должна быть числом.")

    chat_id = update.effective_chat.id
    conn = db()
    with conn:
        cur = conn.execute(
            "SELECT 1 FROM watches WHERE chat_id=? AND address=?",
            (chat_id, addr)
        )
        if not cur.fetchone():
            conn.close()
            return await update.message.reply_text("Сначала добавь адрес: /watch 0x...")

        conn.execute(
            "UPDATE watches SET min_usdc=? WHERE chat_id=? AND address=?",
            (float(val), chat_id, addr)
        )
    conn.close()

    await update.message.reply_text(f"✅ Порог для {addr}: {float(val)} USDC", reply_markup=main_menu_kb())


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_list(update, context)

# ================= ЛИСТ С INLINE-КНОПКАМИ =================

async def show_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    conn = db()
    rows = conn.execute(
        "SELECT address, min_usdc FROM watches WHERE chat_id=? ORDER BY address",
        (chat_id,)
    ).fetchall()
    conn.close()

    if not rows:
        # update.message может быть None если вызвали из callback — учтём ниже
        if update.message:
            return await update.message.reply_text("Список пуст.", reply_markup=main_menu_kb())
        return

    text_lines = ["📌 Отслеживаемые адреса:"]
    buttons = []

    for addr, min_usdc in rows:
        text_lines.append(f"• {addr} — min {float(min_usdc)} USDC")
        buttons.append([
            InlineKeyboardButton("💰 Min", callback_data=f"min:{addr}"),
            InlineKeyboardButton("❌ Unwatch", callback_data=f"del:{addr}"),
        ])

    markup = InlineKeyboardMarkup(buttons)

    if update.message:
        await update.message.reply_text("\n".join(text_lines), reply_markup=markup)
    else:
        # если вызвали из callback — ответим в тот же чат отдельным сообщением
        await context.bot.send_message(chat_id=chat_id, text="\n".join(text_lines), reply_markup=markup)

# ================= КНОПКИ WATCH/LIST ВНИЗУ (ReplyKeyboard) =================

async def on_menu_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()

    if text == "➕ Watch":
        context.user_data[WAITING_ADDR] = True
        context.user_data.pop(WAITING_MIN, None)
        context.user_data.pop(PENDING_MIN_ADDR, None)
        return await update.message.reply_text("Введи адрес 0x... для отслеживания:")

    if text == "📋 List":
        return await show_list(update, context)

    # если это не меню — пробуем обработать как ввод адреса/мина
    await on_free_text(update, context)

# ================= ОБРАБОТКА ВВОДА ТЕКСТА =================

async def on_free_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.message.text or "").strip()

    # ожидание адреса после кнопки Watch
    if context.user_data.get(WAITING_ADDR):
        addr = normalize(txt)
        context.user_data[WAITING_ADDR] = False

        if not ADDR_RE.match(addr):
            return await update.message.reply_text("❌ Неверный адрес. Попробуй ещё раз: 0x...")

        chat_id = update.effective_chat.id

        last_seen_ts = 0
        try:
            trades = fetch_latest_trades(addr, limit=1)
            if trades:
                last_seen_ts = int(trades[0].get("timestamp") or 0)
        except Exception:
            pass

        conn = db()
        with conn:
            cur = conn.execute(
                "SELECT min_usdc FROM watches WHERE chat_id=? AND address=?",
                (chat_id, addr)
            )
            row = cur.fetchone()
            old_min = float(row[0]) if row else 0.0

            conn.execute(
                "INSERT OR REPLACE INTO watches(chat_id, address, last_seen_ts, min_usdc) VALUES(?,?,?,?)",
                (chat_id, addr, last_seen_ts, old_min)
            )
        conn.close()

        return await update.message.reply_text(f"✅ Добавил {addr}", reply_markup=main_menu_kb())

    # ожидание суммы после кнопки Min
    if context.user_data.get(WAITING_MIN):
        addr = context.user_data.get(PENDING_MIN_ADDR)
        if not addr:
            context.user_data[WAITING_MIN] = False
            return await update.message.reply_text("Что-то пошло не так. Открой /list и нажми 💰 Min заново.")

        try:
            val = parse_amount(txt)
            if val < 0:
                raise ValueError
        except Exception:
            return await update.message.reply_text("❌ Введи число, например: 10000")

        chat_id = update.effective_chat.id
        conn = db()
        with conn:
            conn.execute(
                "UPDATE watches SET min_usdc=? WHERE chat_id=? AND address=?",
                (float(val), chat_id, addr)
            )
        conn.close()

        context.user_data[WAITING_MIN] = False
        context.user_data.pop(PENDING_MIN_ADDR, None)

        return await update.message.reply_text(f"✅ Порог для {addr}: {float(val)} USDC", reply_markup=main_menu_kb())

# ================= CALLBACK КНОПКИ (Min / Unwatch) =================

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data or ""
    chat_id = query.message.chat.id

    if data.startswith("del:"):
        addr = data.split(":", 1)[1]

        conn = db()
        with conn:
            cur = conn.execute(
                "DELETE FROM watches WHERE chat_id=? AND address=?",
                (chat_id, addr)
            )
            deleted = cur.rowcount
        conn.close()

        if deleted:
            await query.edit_message_text(f"🛑 Удалил {addr}")
        else:
            await query.edit_message_text("Адрес уже удалён или не найден.")

        # можно сразу показать обновленный список
        return

    if data.startswith("min:"):
        addr = data.split(":", 1)[1]
        context.user_data[WAITING_MIN] = True
        context.user_data[PENDING_MIN_ADDR] = addr
        context.user_data.pop(WAITING_ADDR, None)

        await query.message.reply_text(f"Введи новый порог (USDC) для {addr}.\nНапример: 10000")
        return

# ================= POLL =================

async def poll_job(context: ContextTypes.DEFAULT_TYPE):
    conn = db()
    rows = conn.execute(
        "SELECT chat_id, address, last_seen_ts, min_usdc FROM watches"
    ).fetchall()

    for chat_id, addr, last_ts, min_usdc in rows:
        try:
            trades = fetch_latest_trades(addr)
        except RuntimeError as e:
            if str(e).startswith("RATE_LIMIT:"):
                wait_s = int(str(e).split(":")[1])
                log.warning("Rate limit, sleep %s sec", wait_s)
                await asyncio.sleep(wait_s)
            continue
        except Exception as e:
            log.warning("Fetch error: %s", e)
            continue

        new_all = [t for t in trades if int(t.get("timestamp") or 0) > int(last_ts)]
        if not new_all:
            continue

        max_ts_all = max(int(t.get("timestamp") or 0) for t in new_all)

        for t in sorted(new_all, key=lambda x: int(x.get("timestamp") or 0)):
            if trade_usdc(t) < float(min_usdc):
                continue

            await context.bot.send_message(
                chat_id=chat_id,
                text=f"👤 `{addr}` (min {float(min_usdc)} USDC)\n" + format_trade(t),
                parse_mode="Markdown"
            )

        with conn:
            conn.execute(
                "UPDATE watches SET last_seen_ts=? WHERE chat_id=? AND address=?",
                (max_ts_all, chat_id, addr)
            )

    conn.close()

# ================= MAIN =================

def main():
    if not BOT_TOKEN or BOT_TOKEN == "PASTE_YOUR_TOKEN_HERE":
        raise SystemExit("❌ Вставь токен в BOT_TOKEN.")

    app = Application.builder().token(BOT_TOKEN).build()

    # команды
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("watch", cmd_watch))
    app.add_handler(CommandHandler("unwatch", cmd_unwatch))
    app.add_handler(CommandHandler("min", cmd_min))
    app.add_handler(CommandHandler("list", cmd_list))

    # inline кнопки из списка
    app.add_handler(CallbackQueryHandler(on_button))

    # кнопки меню и обычный текст
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_menu_text))

    # polling
    app.job_queue.run_repeating(poll_job, interval=POLL_INTERVAL_SEC, first=3)

    log.info("Bot started")
    app.run_polling()

if __name__ == "__main__":
    main()
