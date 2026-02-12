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
    return float(s.replace("_", "").replace(",", ""))


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
    usdc = t.get("usdcSize")

    msg = [
        "🧾 *Сделка*",
        f"📌 {title}",
        f"🎯 {outcome}",
        f"🧭 {side}",
    ]

    if usdc:
        try:
            msg.append(f"💵 {round(float(usdc),2)} USDC")
        except:
            pass

    url = polymarket_url(t)
    if url:
        msg.append(f"🌐 [Открыть событие]({url})")

    return "\n".join(msg)

# ================= COMMANDS =================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Команды:\n"
        "/watch 0x...\n"
        "/min 0x... 10000\n"
        "/list"
    )


async def cmd_watch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    addr = normalize(context.args[0])
    chat_id = update.effective_chat.id

    last_seen_ts = 0
    try:
        t = fetch_latest_trades(addr,1)
        if t:
            last_seen_ts = int(t[0].get("timestamp") or 0)
    except:
        pass

    conn = db()
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO watches VALUES(?,?,?,COALESCE((SELECT min_usdc FROM watches WHERE chat_id=? AND address=?),0))",
            (chat_id, addr, last_seen_ts, chat_id, addr)
        )
    conn.close()

    await update.message.reply_text("Добавлен")


async def cmd_min(update: Update, context: ContextTypes.DEFAULT_TYPE):
    addr = normalize(context.args[0])
    val = parse_amount(context.args[1])
    chat_id = update.effective_chat.id

    conn = db()
    with conn:
        conn.execute(
            "UPDATE watches SET min_usdc=? WHERE chat_id=? AND address=?",
            (val,chat_id,addr)
        )
    conn.close()

    await update.message.reply_text("Порог обновлён")


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    conn = db()
    rows = conn.execute(
        "SELECT address,min_usdc FROM watches WHERE chat_id=?",
        (chat_id,)
    ).fetchall()
    conn.close()

    if not rows:
        return await update.message.reply_text("Пусто")

    msg = []
    for a,m in rows:
        msg.append(f"{a} — {m} USDC")

    await update.message.reply_text("\n".join(msg))

# ================= POLL =================

async def poll_job(context: ContextTypes.DEFAULT_TYPE):
    conn = db()
    rows = conn.execute(
        "SELECT chat_id,address,last_seen_ts,min_usdc FROM watches"
    ).fetchall()

    for chat_id,addr,last_ts,min_usdc in rows:
        try:
            trades = fetch_latest_trades(addr)
        except:
            continue

        new=[t for t in trades if int(t.get("timestamp") or 0)>int(last_ts)]
        if not new:
            continue

        max_ts=max(int(t.get("timestamp") or 0) for t in new)

        for t in new:
            if trade_usdc(t)<float(min_usdc):
                continue

            await context.bot.send_message(
                chat_id=chat_id,
                text=f"`{addr}`\n"+format_trade(t),
                parse_mode="Markdown"
            )

        with conn:
            conn.execute(
                "UPDATE watches SET last_seen_ts=? WHERE chat_id=? AND address=?",
                (max_ts,chat_id,addr)
            )

    conn.close()

# ================= MAIN =================

def main():
    app=Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",cmd_start))
    app.add_handler(CommandHandler("watch",cmd_watch))
    app.add_handler(CommandHandler("min",cmd_min))
    app.add_handler(CommandHandler("list",cmd_list))

    app.job_queue.run_repeating(poll_job,interval=POLL_INTERVAL_SEC,first=3)

    log.info("Bot started")
    app.run_polling()

if __name__=="__main__":
    main()
