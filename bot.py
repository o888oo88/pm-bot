import re
import asyncio
import logging
import sqlite3
import requests
from pathlib import Path

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ================= НАСТРОЙКИ =================

BOT_TOKEN = "8273670933:AAHxaLl92JcNm9nfDd2mOlMA8DEMLBiCQpo"
POLL_INTERVAL_SEC = 2

DATA_API = "https://data-api.polymarket.com/activity"

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "watch.db"

ADDR_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("pm-bot")

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
    return float(s.strip().replace("_", "").replace(",", ""))


def trade_usdc(t: dict) -> float:
    try:
        return float(t.get("usdcSize") or 0)
    except:
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

# ================= FORMAT =================

def format_trade(t: dict) -> str:
    title = t.get("title") or "(без названия)"
    side = t.get("side") or "TRADE"
    outcome = t.get("outcome") or "-"
    price = t.get("price")
    usdc = t.get("usdcSize")
    size = t.get("size")
    tx = t.get("transactionHash")

    msg = [
        "🧾 *Сделка*",
        f"📌 *Событие:* {title}",
        f"🎯 *Outcome:* {outcome}",
        f"🧭 *Side:* {side}",
    ]

    if usdc is not None:
        try:
            msg.append(f"💵 *Сумма:* {round(float(usdc), 2)} USDC")
        except:
            pass

    if price is not None:
        msg.append(f"🏷 *Цена:* {price}")

    if size is not None:
        msg.append(f"📦 *Size:* {size}")

    if tx:
        msg.append(f"🔗 *Tx:* `{tx}`")

    url = polymarket_url(t)
    if url:
        msg.append(f"🌐 [Открыть событие]({url})")

    return "\n".join(msg)

# ================= COMMANDS =================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Команды:\n"
        "/watch 0x...\n"
        "/unwatch 0x...\n"
        "/min 0x... 10000\n"
        "/list\n"
    )


async def cmd_watch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("Пример: /watch 0x123...")

    addr = normalize(context.args[0])
    if not ADDR_RE.match(addr):
        return await update.message.reply_text("❌ Неверный адрес.")

    chat_id = update.effective_chat.id

    # чтобы не присылать историю — ставим last_seen на последний трейд
    last_seen_ts = 0
    try:
        t = fetch_latest_trades(addr, 1)
        if t:
            last_seen_ts = int(t[0].get("timestamp") or 0)
    except:
        pass

    conn = db()
    with conn:
        # сохранить старый min_usdc если адрес уже был
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

    await update.message.reply_text(f"✅ Добавил {addr}")


async def cmd_unwatch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("Пример: /unwatch 0x123...")

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

    if deleted:
        await update.message.reply_text(f"🛑 Удалил {addr}")
    else:
        await update.message.reply_text("Адрес не найден.")


async def cmd_min(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        return await update.message.reply_text("Пример: /min 0x123... 10000")

    addr = normalize(context.args[0])
    if not ADDR_RE.match(addr):
        return await update.message.reply_text("❌ Неверный адрес.")

    try:
        val = parse_amount(context.args[1])
    except:
        return await update.message.reply_text("❌ Сумма должна быть числом.")

    chat_id = update.effective_chat.id

    conn = db()
    with conn:
        # убедимся что адрес существует
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

    await update.message.reply_text(f"✅ Порог для {addr}: {float(val)} USDC")


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    conn = db()
    rows = conn.execute(
        "SELECT address, min_usdc FROM watches WHERE chat_id=? ORDER BY address",
        (chat_id,)
    ).fetchall()
    conn.close()

    if not rows:
        return await update.message.reply_text("Список пуст.")

    msg = ["📌 Отслеживаемые адреса:"]
    for a, m in rows:
        msg.append(f"• {a} — порог {float(m)} USDC")

    await update.message.reply_text("\n".join(msg))

# ================= POLL =================

async def poll_job(context: ContextTypes.DEFAULT_TYPE):
    conn = db()
    rows = conn.execute(
        "SELECT chat_id, address, last_seen_ts, min_usdc FROM watches"
    ).fetchall()

    for chat_id, addr, last_ts, min_usdc in rows:
        try:
            trades = fetch_latest_trades(addr)
        except Exception as e:
            log.warning("Fetch error: %s", e)
            continue

        new_all = [t for t in trades if int(t.get("timestamp") or 0) > int(last_ts)]
        if not new_all:
            continue

        # обновляем last_seen по всем новым, чтобы не повторять мелкие сделки
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
        raise SystemExit("❌ Вставь токен в BOT_TOKEN в начале файла.")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("watch", cmd_watch))
    app.add_handler(CommandHandler("unwatch", cmd_unwatch))
    app.add_handler(CommandHandler("min", cmd_min))
    app.add_handler(CommandHandler("list", cmd_list))

    app.job_queue.run_repeating(poll_job, interval=POLL_INTERVAL_SEC, first=3)

    log.info("Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
