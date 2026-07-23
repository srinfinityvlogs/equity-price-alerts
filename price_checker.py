import os
import re
import time
import threading
import requests
import yfinance as yf
import psycopg2
from contextlib import closing
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ALLOWED_CHAT_IDS = [c.strip() for c in os.getenv("ALLOWED_CHAT_IDS", "").split(",") if c.strip()]

CHECK_INTERVAL_SECONDS = 180
COMMAND_POLL_INTERVAL_SECONDS = 5
REMINDER_CHECK_INTERVAL_SECONDS = 30

IST = ZoneInfo("Asia/Kolkata")
MARKET_OPEN = dtime(9, 0)
MARKET_CLOSE = dtime(15, 30)


# ---------- Database setup ----------

def get_conn():
    return psycopg2.connect(
        host=os.getenv("PGHOST"),
        port=os.getenv("PGPORT", "5432"),
        dbname=os.getenv("PGDATABASE"),
        user=os.getenv("PGUSER"),
        password=os.getenv("PGPASSWORD"),
    )


def init_db():
    with closing(get_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS watchlist (
                    chat_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    display_name TEXT,
                    target_price DOUBLE PRECISION NOT NULL,
                    condition TEXT NOT NULL,
                    enabled BOOLEAN DEFAULT TRUE,
                    PRIMARY KEY (chat_id, symbol, condition, target_price)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS alert_state (
                    chat_id TEXT NOT NULL,
                    alert_key TEXT NOT NULL,
                    alerted_date TEXT NOT NULL,
                    PRIMARY KEY (chat_id, alert_key)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bot_offset (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    offset_value BIGINT NOT NULL
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS reminders (
                    chat_id TEXT NOT NULL,
                    remind_time TEXT NOT NULL,
                    target_date TEXT NOT NULL,
                    message TEXT NOT NULL,
                    PRIMARY KEY (chat_id, remind_time, target_date)
                )
            """)
        conn.commit()
    print("[DB] Tables ready.")


# ---------- Watchlist (Postgres-backed) ----------

def load_watchlist(chat_id):
    with closing(get_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT symbol, display_name, target_price, condition, enabled "
                "FROM watchlist WHERE chat_id = %s ORDER BY symbol, condition, target_price",
                (str(chat_id),)
            )
            rows = cur.fetchall()
    return [
        {"symbol": r[0], "display_name": r[1], "target_price": r[2], "condition": r[3], "enabled": r[4]}
        for r in rows
    ]


def stock_exists(chat_id, symbol, condition, target_price):
    with closing(get_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM watchlist WHERE chat_id=%s AND symbol=%s AND condition=%s AND target_price=%s",
                (str(chat_id), symbol, condition, target_price)
            )
            return cur.fetchone() is not None


def add_stock(chat_id, symbol, display_name, target_price, condition):
    with closing(get_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO watchlist (chat_id, symbol, display_name, target_price, condition, enabled)
                VALUES (%s, %s, %s, %s, %s, TRUE)
                ON CONFLICT (chat_id, symbol, condition, target_price) DO NOTHING
                """,
                (str(chat_id), symbol, display_name, target_price, condition)
            )
        conn.commit()


def remove_stock(chat_id, symbol, condition_filter=None, price_filter=None):
    query = "DELETE FROM watchlist WHERE chat_id=%s AND symbol=%s"
    params = [str(chat_id), symbol]
    if condition_filter:
        query += " AND condition=%s"
        params.append(condition_filter)
    if price_filter is not None:
        query += " AND target_price=%s"
        params.append(price_filter)
    with closing(get_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(query, tuple(params))
            removed_count = cur.rowcount
        conn.commit()
    return removed_count


# ---------- Alert state (Postgres-backed) ----------

def is_already_alerted(chat_id, alert_key, today):
    with closing(get_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM alert_state WHERE chat_id=%s AND alert_key=%s AND alerted_date=%s",
                (str(chat_id), alert_key, today)
            )
            return cur.fetchone() is not None


def mark_alerted(chat_id, alert_key, today):
    with closing(get_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO alert_state (chat_id, alert_key, alerted_date)
                VALUES (%s, %s, %s)
                ON CONFLICT (chat_id, alert_key) DO UPDATE SET alerted_date = EXCLUDED.alerted_date
                """,
                (str(chat_id), alert_key, today)
            )
        conn.commit()


# ---------- Reminders (Postgres-backed, ONE-TIME, today only) ----------

def add_reminder(chat_id, remind_time, target_date, message):
    with closing(get_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO reminders (chat_id, remind_time, target_date, message)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (chat_id, remind_time, target_date) DO UPDATE SET message = EXCLUDED.message
                """,
                (str(chat_id), remind_time, target_date, message)
            )
        conn.commit()


def remove_reminder(chat_id, remind_time, target_date):
    with closing(get_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM reminders WHERE chat_id=%s AND remind_time=%s AND target_date=%s",
                (str(chat_id), remind_time, target_date)
            )
            removed = cur.rowcount
        conn.commit()
    return removed


def load_reminders(chat_id, target_date):
    with closing(get_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT remind_time, message FROM reminders WHERE chat_id=%s AND target_date=%s ORDER BY remind_time",
                (str(chat_id), target_date)
            )
            return cur.fetchall()


def load_due_reminders(target_date, current_hm):
    with closing(get_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT chat_id, remind_time, message FROM reminders WHERE target_date=%s AND remind_time=%s",
                (target_date, current_hm)
            )
            return cur.fetchall()


def delete_reminder_exact(chat_id, remind_time, target_date):
    with closing(get_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM reminders WHERE chat_id=%s AND remind_time=%s AND target_date=%s",
                (chat_id, remind_time, target_date)
            )
        conn.commit()


TIME_RE = re.compile(r'^(\d{1,2})(?::(\d{2}))?\s*(AM|PM)?$', re.IGNORECASE)


def parse_time_to_24h(time_str):
    """Parses '2PM', '2:30PM', '14:00', '09:15' into 'HH:MM' 24-hour format, or None if invalid."""
    m = TIME_RE.match(time_str.strip())
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2)) if m.group(2) else 0
    ampm = m.group(3).upper() if m.group(3) else None

    if ampm == "PM" and hour != 12:
        hour += 12
    elif ampm == "AM" and hour == 12:
        hour = 0

    if not (0 <= hour <= 23) or not (0 <= minute <= 59):
        return None

    return f"{hour:02d}:{minute:02d}"


# ---------- Offset (Postgres-backed) ----------

def load_offset():
    with closing(get_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT offset_value FROM bot_offset WHERE id = 1")
            row = cur.fetchone()
    return row[0] if row else 0


def save_offset(offset):
    with closing(get_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO bot_offset (id, offset_value) VALUES (1, %s)
                ON CONFLICT (id) DO UPDATE SET offset_value = EXCLUDED.offset_value
                """,
                (offset,)
            )
        conn.commit()


# ---------- Market hours & price fetching ----------

def is_market_open():
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return False
    current_time = now.time()
    return MARKET_OPEN <= current_time <= MARKET_CLOSE


def get_today_key():
    return datetime.now(IST).strftime("%Y-%m-%d")


def fetch_price(symbol):
    ticker = yf.Ticker(symbol)
    data = ticker.history(period="1d", interval="1m")
    if data.empty:
        return None
    return float(data["Close"].iloc[-1])


def send_telegram_message(chat_id, message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(url, data={"chat_id": chat_id, "text": message}, timeout=10)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"[SEND ERROR] chat={chat_id}: {e}")


def check_watchlist_for_chat(chat_id):
    watchlist = load_watchlist(chat_id)
    today = get_today_key()

    for stock in watchlist:
        if not stock.get("enabled", True):
            continue
        symbol = stock["symbol"]
        target = stock["target_price"]
        condition = stock["condition"]
        display_name = stock.get("display_name") or symbol

        price = fetch_price(symbol)
        if price is None:
            print(f"[WARN] chat={chat_id}: No price data for {symbol}")
            continue

        alert_key = f"{symbol}:{condition}:{target}"
        already_alerted = is_already_alerted(chat_id, alert_key, today)
        triggered = (
            (condition == "above" and price > target) or
            (condition == "below" and price < target)
        )

        print(f"chat={chat_id} {symbol}: Rs.{price:.2f} (target {condition} Rs.{target}) - triggered={triggered}, already_alerted={already_alerted}")

        if triggered and not already_alerted:
            direction = "risen above" if condition == "above" else "fallen below"
            message = (
                f"📈 Price Alert: {display_name}\n"
                f"Current: ₹{price:.2f}\n"
                f"Has {direction} your target of ₹{target}"
            )
            send_telegram_message(chat_id, message)
            mark_alerted(chat_id, alert_key, today)
            print(f"[ALERTED] chat={chat_id} {symbol} ({condition} {target})")


# ---------- Chat command handling ----------

def get_updates(offset):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    params = {"offset": offset, "timeout": 10}
    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        return resp.json().get("result", [])
    except requests.RequestException as e:
        print(f"[POLL ERROR] {e}")
        return []


def is_valid_symbol(symbol):
    try:
        price = fetch_price(symbol)
        return price is not None
    except Exception:
        return False


def handle_command(text, chat_id):
    chat_id_str = str(chat_id)
    if chat_id_str not in ALLOWED_CHAT_IDS:
        print(f"[IGNORED] Command from unauthorized chat_id={chat_id}")
        return

    parts = text.strip().split()
    if not parts:
        return

    command = parts[0].lower()
    rest = parts[1:]
    today = get_today_key()

    if command == "/addstock":
        if len(parts) != 4:
            send_telegram_message(chat_id, "Usage: /addstock <SYMBOL> <TARGET_PRICE> <above|below>\nExample: /addstock RELIANCE.NS 3000 above")
            return
        symbol, price_str, condition = parts[1].upper(), parts[2], parts[3].lower()
        if condition not in ("above", "below"):
            send_telegram_message(chat_id, "Condition must be 'above' or 'below'.")
            return
        try:
            target_price = float(price_str)
        except ValueError:
            send_telegram_message(chat_id, f"'{price_str}' is not a valid number.")
            return
        send_telegram_message(chat_id, f"Checking if {symbol} is a valid symbol...")
        if not is_valid_symbol(symbol):
            send_telegram_message(chat_id, f"Couldn't find price data for '{symbol}'. Make sure it's a valid ticker (NSE needs '.NS').")
            return
        if stock_exists(chat_id, symbol, condition, target_price):
            send_telegram_message(chat_id, f"'{symbol} {condition} ₹{target_price}' is already on your watchlist.")
            return
        display_name = symbol.replace(".NS", "").replace(".BO", "")
        add_stock(chat_id, symbol, display_name, target_price, condition)
        send_telegram_message(chat_id, f"✅ Added {symbol}: alert when price goes {condition} ₹{target_price}")

    elif command == "/removestock":
        if len(parts) not in (2, 3, 4):
            send_telegram_message(chat_id, "Usage: /removestock <SYMBOL> [above|below] [PRICE]")
            return
        symbol = parts[1].upper()
        condition_filter = parts[2].lower() if len(parts) >= 3 else None
        price_filter = None
        if condition_filter and condition_filter not in ("above", "below"):
            send_telegram_message(chat_id, "Condition must be 'above' or 'below'.")
            return
        if len(parts) == 4:
            try:
                price_filter = float(parts[3])
            except ValueError:
                send_telegram_message(chat_id, f"'{parts[3]}' is not a valid number.")
                return
        removed_count = remove_stock(chat_id, symbol, condition_filter, price_filter)
        if removed_count == 0:
            send_telegram_message(chat_id, f"No matching entry found for '{symbol}'.")
        else:
            send_telegram_message(chat_id, f"✅ Removed {removed_count} matching entry(ies) for {symbol}.")

    elif command == "/liststocks":
        watchlist = load_watchlist(chat_id)
        if not watchlist:
            send_telegram_message(chat_id, "Watchlist is empty.")
            return
        lines = ["📋 Current watchlist:"]
        for s in watchlist:
            status = "" if s.get("enabled", True) else " (disabled)"
            lines.append(f"- {s['symbol']}: {s['condition']} ₹{s['target_price']}{status}")
        send_telegram_message(chat_id, "\n".join(lines))

    elif command == "/remindme":
        if not rest:
            send_telegram_message(
                chat_id,
                "Usage: /remindme <TIME> [message]\n"
                "Time formats: 2PM, 2:30PM, 14:00, 09:15 (no space before AM/PM)\n"
                "Example: /remindme 2PM Check the market\n"
                "This is a ONE-TIME reminder for today only."
            )
            return
        time_str = rest[0]
        message_text = " ".join(rest[1:]) if len(rest) > 1 else "⏰ Reminder: check the market!"
        parsed_time = parse_time_to_24h(time_str)
        if parsed_time is None:
            send_telegram_message(chat_id, f"Couldn't understand time '{time_str}'. Try formats like 2PM, 2:30PM, or 14:00.")
            return
        current_hm = datetime.now(IST).strftime("%H:%M")
        if parsed_time <= current_hm:
            send_telegram_message(chat_id, f"'{parsed_time}' has already passed today ({current_hm} now). Reminder not set.")
            return
        add_reminder(chat_id, parsed_time, today, message_text)
        send_telegram_message(chat_id, f"✅ One-time reminder set for {parsed_time} IST TODAY: \"{message_text}\"")

    elif command == "/removereminder":
        if not rest:
            send_telegram_message(chat_id, "Usage: /removereminder <TIME>\nExample: /removereminder 2PM")
            return
        parsed_time = parse_time_to_24h(rest[0])
        if parsed_time is None:
            send_telegram_message(chat_id, f"Couldn't understand time '{rest[0]}'.")
            return
        removed = remove_reminder(chat_id, parsed_time, today)
        if removed:
            send_telegram_message(chat_id, f"✅ Removed today's reminder at {parsed_time}.")
        else:
            send_telegram_message(chat_id, f"No reminder found at {parsed_time} for today.")

    elif command == "/listreminders":
        reminders = load_reminders(chat_id, today)
        if not reminders:
            send_telegram_message(chat_id, "No reminders set for today.")
            return
        lines = ["⏰ Today's reminders:"]
        for remind_time, message in reminders:
            lines.append(f"- {remind_time}: {message}")
        send_telegram_message(chat_id, "\n".join(lines))

    elif command == "/help":
        send_telegram_message(
            chat_id,
            "Stock commands:\n"
            "/addstock <SYMBOL> <PRICE> <above|below>\n"
            "/removestock <SYMBOL> [above|below] [PRICE]\n"
            "/liststocks\n\n"
            "Symbol format:\n"
            "NSE: TICKER.NS (e.g. RELIANCE.NS)\n"
            "BSE: TICKER.BO (e.g. AFCOM.BO) - use the trading symbol, "
            "NOT the numeric BSE scrip code from screener.in/BSE India\n\n"
            "Reminder commands (one-time, today only):\n"
            "/remindme <TIME> [message] - e.g. /remindme 2PM Check market\n"
            "/removereminder <TIME>\n"
            "/listreminders\n\n"
            "Market hours: 9:00 AM-3:30 PM IST, Mon-Fri.\n"
            "Your data here is independent from any other group."
        )


def poll_commands_loop():
    offset = load_offset()
    print("Listening for chat commands...")
    while True:
        updates = get_updates(offset)
        for update in updates:
            offset = update["update_id"] + 1
            save_offset(offset)
            message = update.get("message", {})
            text = message.get("text", "")
            chat_id = message.get("chat", {}).get("id")
            if text.startswith("/"):
                handle_command(text, chat_id)
        time.sleep(COMMAND_POLL_INTERVAL_SECONDS)


# ---------- Price checking loop ----------

def price_check_loop():
    print("Starting price checker...")
    while True:
        try:
            if is_market_open():
                for chat_id in ALLOWED_CHAT_IDS:
                    check_watchlist_for_chat(chat_id)
            else:
                print("[MARKET CLOSED] Skipping check cycle")
        except Exception as e:
            print(f"[UNEXPECTED ERROR] {e} - continuing after short pause")
            time.sleep(10)
            continue
        time.sleep(CHECK_INTERVAL_SECONDS)


# ---------- Reminder loop (one-time, fires then deletes) ----------

def reminder_loop():
    print("Starting reminder checker...")
    while True:
        try:
            now = datetime.now(IST)
            current_hm = now.strftime("%H:%M")
            today = get_today_key()
            for chat_id, remind_time, message in load_due_reminders(today, current_hm):
                send_telegram_message(chat_id, message)
                delete_reminder_exact(chat_id, remind_time, today)
                print(f"[REMINDER SENT] chat={chat_id} at {remind_time}")
        except Exception as e:
            print(f"[REMINDER ERROR] {e}")
        time.sleep(REMINDER_CHECK_INTERVAL_SECONDS)


# ---------- Run all loops concurrently ----------

def main():
    if not ALLOWED_CHAT_IDS:
        print("[FATAL] No ALLOWED_CHAT_IDS configured. Set it in .env, comma-separated.")
        return

    required = ["PGHOST", "PGDATABASE", "PGUSER", "PGPASSWORD"]
    missing = [v for v in required if not os.getenv(v)]
    if missing:
        print(f"[FATAL] Missing database env vars: {missing}")
        return

    init_db()

    print(f"Configured for {len(ALLOWED_CHAT_IDS)} chat(s): {ALLOWED_CHAT_IDS}")

    threading.Thread(target=price_check_loop, daemon=True).start()
    threading.Thread(target=poll_commands_loop, daemon=True).start()
    threading.Thread(target=reminder_loop, daemon=True).start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("Stopped by user.")


if __name__ == "__main__":
    main()