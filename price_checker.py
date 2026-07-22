import os
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
        {
            "symbol": r[0],
            "display_name": r[1],
            "target_price": r[2],
            "condition": r[3],
            "enabled": r[4],
        }
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

    if command == "/addstock":
        if len(parts) != 4:
            send_telegram_message(
                chat_id,
                "Usage: /addstock <SYMBOL> <TARGET_PRICE> <above|below>\n"
                "Example: /addstock RELIANCE.NS 3000 above\n"
                "You can add multiple different targets for the same stock "
                "and same condition (e.g. above 580 AND above 600)."
            )
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
            send_telegram_message(
                chat_id,
                f"Couldn't find price data for '{symbol}'. Make sure it's a valid ticker (NSE needs '.NS')."
            )
            return

        if stock_exists(chat_id, symbol, condition, target_price):
            send_telegram_message(chat_id, f"'{symbol} {condition} ₹{target_price}' is already on your watchlist.")
            return

        display_name = symbol.replace(".NS", "").replace(".BO", "")
        add_stock(chat_id, symbol, display_name, target_price, condition)
        send_telegram_message(chat_id, f"✅ Added {symbol}: alert when price goes {condition} ₹{target_price}")

    elif command == "/removestock":
        if len(parts) not in (2, 3, 4):
            send_telegram_message(
                chat_id,
                "Usage: /removestock <SYMBOL> [above|below] [PRICE]\n"
                "- SYMBOL only: removes ALL targets for that stock\n"
                "- SYMBOL + above/below: removes all targets with that condition\n"
                "- SYMBOL + above/below + PRICE: removes just that one exact target"
            )
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

    elif command == "/help":
        send_telegram_message(
            chat_id,
            "Commands:\n"
            "/addstock <SYMBOL> <PRICE> <above|below>\n"
            "/removestock <SYMBOL> [above|below] [PRICE]\n"
            "/liststocks\n\n"
            "A stock can have multiple targets, including several 'above' "
            "or several 'below' at different prices.\n\n"
            "Market hours: 9:00 AM-3:30 PM IST, Mon-Fri.\n"
            "Your watchlist here is independent from any other group."
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


# ---------- Run both loops concurrently ----------

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

    price_thread = threading.Thread(target=price_check_loop, daemon=True)
    command_thread = threading.Thread(target=poll_commands_loop, daemon=True)
    price_thread.start()
    command_thread.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("Stopped by user.")


if __name__ == "__main__":
    main()
