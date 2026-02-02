import re
import asyncio
import logging
import sqlite3
import requests

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ================= НАСТРОЙКИ =================

BOT_TOKEN = "8273670933:AAHxaLl92JcNm9nfDd2mOlMA8DEMLBiCQpo"
POLL_INTERVAL_SEC = 2

DATA_API = "https://data-api.polymarket.com/activity"
DB_PATH = "watch.db"

ADDR_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("pm-bot")

# ================= БАЗА =================

def ensure_schema(conn: sqlite3.Connection):
    # базовая таблица
    conn.execute("""
        CREATE TABLE IF NOT EXISTS watches (
            chat_id INTEGER NOT NULL,
            address TEXT NOT NULL,
            last_seen_ts INTEGER NOT NULL DEFAULT 0,
            min_usdc REAL NOT NULL DEFAULT 0,
            PRIMARY KEY (chat_id, address)
        )
    """)

    # миграция для старых БД (если вдруг таблица была без min_usdc)
    cols = [row[1] for row in conn.execute("PRAGMA table_info(watches)").fetchall()]
    if "min_usdc" not in cols:
        conn.execute("ALTER TABLE watches ADD COLUMN min_usdc REAL NOT NULL DEFAULT 0")


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    ensure_schema(conn)
    return conn


def normalize(addr: str) -> str:
    return addr.lower().strip()


def parse_amount(s: str) -> float:
    # поддержка 10_000 и 10,000
    s = s.strip().replace("_", "").replace(",", "")
    return float(s)


def trade_usdc(t: dict) -> float:
    try:
        return float(t.get("usdcSize") or 0)
    except Exception:
        return 0.0

# ================= POLYMARKET API =================

def fetch_latest_trades(address: str, limit: int = 30):
    params = {
        "user": address,
        "type": "TRADE",
        "limit": limit,
        "offset": 0,
        "sortBy": "TIMESTAMP",
        "sortDirection": "DESC",
    }

    r = requests.get(DATA_API, params=params, timeout=15)

    if r.status_code == 429:
        retry_after = int(r.headers.get("Retry-After", "2"))
        raise RuntimeError(f"RATE_LIMIT:{retry_after}")

    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else []

# ================= ФОРМАТ СИГНАЛА =================

def format_trade(t: dict) -> str:
    title = t.get("title") or "(без названия)"
    side = t.get("side") or "TRADE"
    outcome = t.get("outcome") or "(не указан)"
    price = t.get("price")
    usdc = t.get("usdcSize")
    size = t.get("size")
    tx = t.get("transactionHash")

    lines = [
        "🧾 *Новая сделка*",
        f"📌 *Событие:* {title}",
        f"🎯 *Куда ставка:* {outcome}",
        f"🧭 *Действие:* {side}",
    ]

    if usdc is not None:
        try:
            usdc_val = round(float(usdc), 2)
            lines.append(f"💵 *Сумма:* {usdc_val} USDC")
        except Exception:
            pass

    if price is not None:
        lines.append(f"🏷 *Цена:* {price}")

    if size is not None:
        lines.append(f"📦 *Size:* {size}")

    if tx:
        lines.append(f"🔗 *Tx:* `{tx}`")

    return "\n".join(lines)

# ================= TELEGRAM COMMANDS =================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Polymarket Signal Bot\n\n"
        "Команды:\n"
        "/watch 0x...          — начать отслеживание адреса\n"
        "/unwatch 0x...        — убрать адрес\n"
        "/list                — список адресов + пороги\n"
        "/min 0x... 10000      — порог алертов для адреса (USDC)\n\n"
        "Пример:\n"
        "/watch 0x1234...\n"
        "/min 0x1234... 10000"
    )


async def cmd_watch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("Пример: /watch 0x1234...")

    addr = normalize(context.args[0])
    if not ADDR_RE.match(addr):
        return await update.message.reply_text("❌ Неверный формат адреса.")

    chat_id = update.effective_chat.id

    # чтобы не спамить старыми — ставим last_seen на последний трейд
    last_seen_ts = 0
    try:
        trades = fetch_latest_trades(addr, limit=1)
        if trades:
            last_seen_ts = int(trades[0].get("timestamp") or 0)
    except Exception:
        pass

    conn = db()
    with conn:
        # если уже был адрес — сохраняем существующий min_usdc, иначе 0
        cur = conn.execute(
            "SELECT min_usdc FROM watches WHERE chat_id=? AND address=?",
            (chat_id, addr)
        )
        row = cur.fetchone()
        min_usdc = float(row[0]) if row else 0.0

        conn.execute(
            "INSERT OR REPLACE INTO watches(chat_id, address, last_seen_ts, min_usdc) VALUES(?,?,?,?)",
            (chat_id, addr, last_seen_ts, min_usdc)
        )
    conn.close()

    await update.message.reply_text(
        f"✅ Начал следить за {addr}\n"
        f"Порог: {min_usdc} USDC\n"
        f"Установить: /min {addr} 10000"
    )


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

    if deleted:
        await update.message.reply_text(f"🛑 Убрал {addr}")
    else:
        await update.message.reply_text("Адрес не найден.")


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    conn = db()
    cur = conn.execute(
        "SELECT address, min_usdc FROM watches WHERE chat_id=? ORDER BY address",
        (chat_id,)
    )
    rows = cur.fetchall()
    conn.close()

    if not rows:
        return await update.message.reply_text("Список пуст.")

    msg = ["📌 Отслеживаемые адреса:"]
    for addr, min_usdc in rows:
        msg.append(f"• {addr}  —  порог: {float(min_usdc)} USDC")

    await update.message.reply_text("\n".join(msg))


async def cmd_min(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # /min <address> <amount>
    if len(context.args) < 2:
        return await update.message.reply_text("Пример: /min 0x1234... 10000")

    addr = normalize(context.args[0])
    if not ADDR_RE.match(addr):
        return await update.message.reply_text("❌ Неверный формат адреса.")

    try:
        value = parse_amount(context.args[1])
        if value < 0:
            raise ValueError("negative")
    except Exception:
        return await update.message.reply_text("❌ Сумма должна быть числом. Пример: /min 0x1234... 10000")

    chat_id = update.effective_chat.id

    conn = db()
    with conn:
        # проверим, что адрес уже добавлен
        cur = conn.execute(
            "SELECT 1 FROM watches WHERE chat_id=? AND address=?",
            (chat_id, addr)
        )
        if not cur.fetchone():
            conn.close()
            return await update.message.reply_text("Сначала добавь адрес: /watch 0x...")

        conn.execute(
            "UPDATE watches SET min_usdc=? WHERE chat_id=? AND address=?",
            (float(value), chat_id, addr)
        )
    conn.close()

    await update.message.reply_text(f"✅ Порог для {addr}: {float(value)} USDC")

# ================= POLLING JOB =================

async def poll_job(context: ContextTypes.DEFAULT_TYPE):
    conn = db()
    cur = conn.execute("SELECT chat_id, address, last_seen_ts, min_usdc FROM watches")
    watches = cur.fetchall()

    for chat_id, addr, last_ts, min_usdc in watches:
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

        # все новые сделки (для обновления last_seen_ts)
        new_all = [
            t for t in trades
            if int(t.get("timestamp") or 0) > int(last_ts)
        ]
        if not new_all:
            continue

        # обновим last_seen_ts по всем новым — чтобы мелочь не повторялась бесконечно
        max_ts_all = max(int(t.get("timestamp") or 0) for t in new_all)

        # алерт только по порогу этого адреса
        new_alerts = [t for t in new_all if trade_usdc(t) >= float(min_usdc)]

        if new_alerts:
            new_alerts.sort(key=lambda x: int(x.get("timestamp") or 0))
            for t in new_alerts:
                text = f"👤 `{addr}` (min {float(min_usdc)} USDC)\n" + format_trade(t)
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode="Markdown"
                )

        with conn:
            conn.execute(
                "UPDATE watches SET last_seen_ts=? WHERE chat_id=? AND address=?",
                (max_ts_all, chat_id, addr)
            )

    conn.close()

# ================= START =================

def main():
    if not BOT_TOKEN or BOT_TOKEN == "PASTE_YOUR_BOT_TOKEN_HERE":
        raise SystemExit("❌ Вставь токен в BOT_TOKEN в начале файла.")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("watch", cmd_watch))
    app.add_handler(CommandHandler("unwatch", cmd_unwatch))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("min", cmd_min))

    app.job_queue.run_repeating(poll_job, interval=POLL_INTERVAL_SEC, first=3)

    log.info("Bot started")
    app.run_polling()

if __name__ == "__main__":
    main()
