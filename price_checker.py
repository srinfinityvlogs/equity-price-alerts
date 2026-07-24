import os
import re
import time
import threading
import requests
import yfinance as yf
import psycopg2
from contextlib import closing
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ALLOWED_CHAT_IDS = [c.strip() for c in os.getenv("ALLOWED_CHAT_IDS", "").split(",") if c.strip()]
BROADCAST_CHAT_IDS = [c.strip() for c in os.getenv("BROADCAST_CHAT_IDS", "").split(",") if c.strip()]
ADMIN_TELEGRAM_USER_ID = os.getenv("ADMIN_TELEGRAM_USER_ID", "").strip()
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "").strip()  # where relayed replies land (your own group)

GROUP_LABELS = {}
for pair in os.getenv("GROUP_LABELS", "").split(","):
    pair = pair.strip()
    if not pair or ":" not in pair:
        continue
    label, cid = pair.split(":", 1)
    GROUP_LABELS[label.strip().upper()] = cid.strip()

CHECK_INTERVAL_SECONDS = 180
COMMAND_POLL_INTERVAL_SECONDS = 5
REMINDER_CHECK_INTERVAL_SECONDS = 30

IST = ZoneInfo("Asia/Kolkata")
MARKET_OPEN = dtime(9, 0)
MARKET_CLOSE = dtime(15, 30)


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
                    chat_id TEXT NOT NULL, symbol TEXT NOT NULL, display_name TEXT,
                    target_price DOUBLE PRECISION NOT NULL, condition TEXT NOT NULL,
                    enabled BOOLEAN DEFAULT TRUE,
                    PRIMARY KEY (chat_id, symbol, condition, target_price)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS alert_state (
                    chat_id TEXT NOT NULL, alert_key TEXT NOT NULL, alerted_date TEXT NOT NULL,
                    PRIMARY KEY (chat_id, alert_key)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bot_offset (
                    id INTEGER PRIMARY KEY DEFAULT 1, offset_value BIGINT NOT NULL
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS reminders (
                    chat_id TEXT NOT NULL, remind_time TEXT NOT NULL, target_date TEXT NOT NULL,
                    message TEXT NOT NULL,
                    PRIMARY KEY (chat_id, remind_time, target_date)
                )
            """)
        conn.commit()
    print("[DB] Tables ready.")


def load_watchlist(chat_id):
    with closing(get_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT symbol, display_name, target_price, condition, enabled "
                "FROM watchlist WHERE chat_id = %s ORDER BY symbol, condition, target_price",
                (str(chat_id),)
            )
            rows = cur.fetchall()
    return [{"symbol": r[0], "display_name": r[1], "target_price": r[2], "condition": r[3], "enabled": r[4]} for r in rows]


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
                """INSERT INTO watchlist (chat_id, symbol, display_name, target_price, condition, enabled)
                VALUES (%s, %s, %s, %s, %s, TRUE)
                ON CONFLICT (chat_id, symbol, condition, target_price) DO NOTHING""",
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
                """INSERT INTO alert_state (chat_id, alert_key, alerted_date) VALUES (%s, %s, %s)
                ON CONFLICT (chat_id, alert_key) DO UPDATE SET alerted_date = EXCLUDED.alerted_date""",
                (str(chat_id), alert_key, today)
            )
        conn.commit()


def add_reminder(chat_id, remind_time, target_date, message):
    with closing(get_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO reminders (chat_id, remind_time, target_date, message) VALUES (%s, %s, %s, %s)
                ON CONFLICT (chat_id, remind_time, target_date) DO UPDATE SET message = EXCLUDED.message""",
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


def load_reminders(chat_id, target_date=None):
    with closing(get_conn()) as conn:
        with conn.cursor() as cur:
            if target_date:
                cur.execute(
                    "SELECT remind_time, target_date, message FROM reminders WHERE chat_id=%s AND target_date=%s ORDER BY remind_time",
                    (str(chat_id), target_date)
                )
            else:
                cur.execute(
                    "SELECT remind_time, target_date, message FROM reminders WHERE chat_id=%s ORDER BY target_date, remind_time",
                    (str(chat_id),)
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
DATE_RE = re.compile(r'^(\d{1,2})-(\d{1,2})-(\d{4})$')


def parse_time_to_24h(time_str):
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


def parse_date_token(token, now_ist):
    t = token.strip().lower()
    if t == "today":
        return now_ist.strftime("%Y-%m-%d")
    if t == "tomorrow":
        return (now_ist + timedelta(days=1)).strftime("%Y-%m-%d")
    m = DATE_RE.match(token.strip())
    if m:
        day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            d = datetime(year, month, day)
            return d.strftime("%Y-%m-%d")
        except ValueError:
            return None
    return None


def format_date_for_display(date_str, now_ist):
    today_str = now_ist.strftime("%Y-%m-%d")
    tomorrow_str = (now_ist + timedelta(days=1)).strftime("%Y-%m-%d")
    if date_str == today_str:
        return "today"
    if date_str == tomorrow_str:
        return "tomorrow"
    return date_str


def resolve_label(label):
    return GROUP_LABELS.get(label.strip().upper())


def label_for_chat_id(chat_id):
    chat_id_str = str(chat_id)
    for label, cid in GROUP_LABELS.items():
        if cid == chat_id_str:
            return label
    return chat_id_str


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
                """INSERT INTO bot_offset (id, offset_value) VALUES (1, %s)
                ON CONFLICT (id) DO UPDATE SET offset_value = EXCLUDED.offset_value""",
                (offset,)
            )
        conn.commit()


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


def send_telegram_photo(chat_id, file_id, caption=None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
    data = {"chat_id": chat_id, "photo": file_id}
    if caption:
        data["caption"] = caption
    try:
        resp = requests.post(url, data=data, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"[PHOTO SEND ERROR] chat={chat_id}: {e}")


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
        triggered = (condition == "above" and price > target) or (condition == "below" and price < target)
        print(f"chat={chat_id} {symbol}: Rs.{price:.2f} (target {condition} Rs.{target}) - triggered={triggered}, already_alerted={already_alerted}")
        if triggered and not already_alerted:
            direction = "risen above" if condition == "above" else "fallen below"
            message = f"📈 Price Alert: {display_name}\nCurrent: ₹{price:.2f}\nHas {direction} your target of ₹{target}"
            send_telegram_message(chat_id, message)
            mark_alerted(chat_id, alert_key, today)
            print(f"[ALERTED] chat={chat_id} {symbol} ({condition} {target})")


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


def is_admin(from_user_id):
    return bool(ADMIN_TELEGRAM_USER_ID) and str(from_user_id) == ADMIN_TELEGRAM_USER_ID


def relay_reply_to_admin(chat_id, message):
    if not ADMIN_CHAT_ID:
        return
    if str(chat_id) == ADMIN_CHAT_ID:
        return  # don't relay replies happening in your own group back to yourself
    sender = message.get("from", {})
    sender_name = sender.get("first_name", "") or sender.get("username", "") or "Someone"
    reply_text = message.get("text") or message.get("caption") or "(non-text message)"
    label = label_for_chat_id(chat_id)
    relayed = f"💬 Reply from {label} ({sender_name}):\n{reply_text}"
    send_telegram_message(ADMIN_CHAT_ID, relayed)
    print(f"[REPLY RELAYED] from chat={chat_id} label={label} sender={sender_name}")


def handle_command(text, chat_id, from_user_id, photo_file_id=None):
    chat_id_str = str(chat_id)
    if chat_id_str not in ALLOWED_CHAT_IDS:
        print(f"[IGNORED] Command from unauthorized chat_id={chat_id}")
        return

    parts = text.strip().split()
    if not parts:
        return

    command = parts[0].lower()
    rest = parts[1:]
    now_ist = datetime.now(IST)
    today = now_ist.strftime("%Y-%m-%d")

    if command == "/addstock":
        if len(parts) != 4:
            send_telegram_message(chat_id, "Usage: /addstock <SYMBOL> <TARGET_PRICE> <above|below>")
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
            send_telegram_message(chat_id, f"Couldn't find price data for '{symbol}'. NSE needs '.NS', BSE needs '.BO' with the ticker symbol, not the numeric scrip code.")
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
            send_telegram_message(chat_id, "Usage: /remindme [DATE] <TIME> [message]")
            return
        maybe_date = parse_date_token(rest[0], now_ist)
        if maybe_date is not None:
            target_date = maybe_date
            if len(rest) < 2:
                send_telegram_message(chat_id, "Missing time.")
                return
            time_str = rest[1]
            message_tokens = rest[2:]
        else:
            target_date = today
            time_str = rest[0]
            message_tokens = rest[1:]
        message_text = " ".join(message_tokens) if message_tokens else "⏰ Reminder: check the market!"
        parsed_time = parse_time_to_24h(time_str)
        if parsed_time is None:
            send_telegram_message(chat_id, f"Couldn't understand time '{time_str}'.")
            return
        current_hm = now_ist.strftime("%H:%M")
        if target_date == today and parsed_time <= current_hm:
            send_telegram_message(chat_id, f"'{parsed_time}' has already passed today.")
            return
        if target_date < today:
            send_telegram_message(chat_id, f"'{target_date}' is in the past.")
            return
        add_reminder(chat_id, parsed_time, target_date, message_text)
        display_date = format_date_for_display(target_date, now_ist)
        send_telegram_message(chat_id, f"✅ One-time reminder set for {parsed_time} IST on {display_date}: \"{message_text}\"")

    elif command == "/removereminder":
        if not rest:
            send_telegram_message(chat_id, "Usage: /removereminder [DATE] <TIME>")
            return
        maybe_date = parse_date_token(rest[0], now_ist)
        if maybe_date is not None:
            target_date = maybe_date
            if len(rest) < 2:
                send_telegram_message(chat_id, "Missing time.")
                return
            time_str = rest[1]
        else:
            target_date = today
            time_str = rest[0]
        parsed_time = parse_time_to_24h(time_str)
        if parsed_time is None:
            send_telegram_message(chat_id, f"Couldn't understand time '{time_str}'.")
            return
        removed = remove_reminder(chat_id, parsed_time, target_date)
        display_date = format_date_for_display(target_date, now_ist)
        if removed:
            send_telegram_message(chat_id, f"✅ Removed reminder at {parsed_time} on {display_date}.")
        else:
            send_telegram_message(chat_id, f"No reminder found at {parsed_time} on {display_date}.")

    elif command == "/listreminders":
        target_date = None
        if rest:
            maybe_date = parse_date_token(rest[0], now_ist)
            if maybe_date is not None:
                target_date = maybe_date
        reminders = load_reminders(chat_id, target_date)
        if not reminders:
            send_telegram_message(chat_id, "No reminders set.")
            return
        lines = ["⏰ Your reminders:"]
        for remind_time, r_date, message in reminders:
            display_date = format_date_for_display(r_date, now_ist)
            lines.append(f"- {display_date} {remind_time}: {message}")
        send_telegram_message(chat_id, "\n".join(lines))

    elif command == "/broadcast":
        if not is_admin(from_user_id):
            send_telegram_message(chat_id, "You're not authorized to use this command.")
            print(f"[BROADCAST DENIED] user_id={from_user_id}")
            return
        if not BROADCAST_CHAT_IDS:
            send_telegram_message(chat_id, "No BROADCAST_CHAT_IDS configured.")
            return
        caption_or_text = " ".join(rest) if rest else None
        if not photo_file_id and not caption_or_text:
            send_telegram_message(chat_id, "Usage: /broadcast <message>\nOr attach an image with /broadcast <caption> as the caption.")
            return
        for target_chat_id in BROADCAST_CHAT_IDS:
            if photo_file_id:
                send_telegram_photo(target_chat_id, photo_file_id, caption_or_text)
            else:
                send_telegram_message(target_chat_id, caption_or_text)
        kind = "image" if photo_file_id else "message"
        send_telegram_message(chat_id, f"✅ Broadcast ({kind}) sent to {len(BROADCAST_CHAT_IDS)} group(s).")
        print(f"[BROADCAST] kind={kind} to={BROADCAST_CHAT_IDS} by user_id={from_user_id}")

    elif command == "/sendto":
        if not is_admin(from_user_id):
            send_telegram_message(chat_id, "You're not authorized to use this command.")
            print(f"[SENDTO DENIED] user_id={from_user_id}")
            return
        if not rest:
            send_telegram_message(chat_id, f"Usage: /sendto <LABEL> <message>\nOr attach an image with /sendto <LABEL> <caption> as the caption.\nKnown labels: {', '.join(GROUP_LABELS.keys()) or 'none configured'}")
            return
        label = rest[0]
        target_chat_id = resolve_label(label)
        if not target_chat_id:
            send_telegram_message(chat_id, f"Unknown label '{label}'. Known labels: {', '.join(GROUP_LABELS.keys()) or 'none configured'}")
            return
        message_text = " ".join(rest[1:]) if len(rest) > 1 else None
        if photo_file_id:
            send_telegram_photo(target_chat_id, photo_file_id, message_text)
        else:
            if not message_text:
                send_telegram_message(chat_id, "Provide a message, or attach an image with a caption.")
                return
            send_telegram_message(target_chat_id, message_text)
        send_telegram_message(chat_id, f"✅ Sent to {label}.")
        print(f"[SENDTO] label={label} chat_id={target_chat_id} by user_id={from_user_id}")

    elif command == "/addstockfor":
        if not is_admin(from_user_id):
            send_telegram_message(chat_id, "You're not authorized to use this command.")
            print(f"[ADDSTOCKFOR DENIED] user_id={from_user_id}")
            return
        if len(rest) != 4:
            send_telegram_message(chat_id, f"Usage: /addstockfor <LABEL> <SYMBOL> <PRICE> <above|below>\nKnown labels: {', '.join(GROUP_LABELS.keys()) or 'none configured'}")
            return
        label = rest[0]
        target_chat_id = resolve_label(label)
        if not target_chat_id:
            send_telegram_message(chat_id, f"Unknown label '{label}'.")
            return
        symbol, price_str, condition = rest[1].upper(), rest[2], rest[3].lower()
        if condition not in ("above", "below"):
            send_telegram_message(chat_id, "Condition must be 'above' or 'below'.")
            return
        try:
            target_price = float(price_str)
        except ValueError:
            send_telegram_message(chat_id, f"'{price_str}' is not a valid number.")
            return
        if not is_valid_symbol(symbol):
            send_telegram_message(chat_id, f"Couldn't find price data for '{symbol}'.")
            return
        if stock_exists(target_chat_id, symbol, condition, target_price):
            send_telegram_message(chat_id, f"'{symbol} {condition} ₹{target_price}' is already on {label}'s watchlist.")
            return
        display_name = symbol.replace(".NS", "").replace(".BO", "")
        add_stock(target_chat_id, symbol, display_name, target_price, condition)
        send_telegram_message(chat_id, f"✅ Added {symbol} to {label}'s watchlist: alert when price goes {condition} ₹{target_price}")
        print(f"[ADDSTOCKFOR] label={label} chat_id={target_chat_id} {symbol} {condition} {target_price} by user_id={from_user_id}")

    elif command == "/remindfor":
        if not is_admin(from_user_id):
            send_telegram_message(chat_id, "You're not authorized to use this command.")
            print(f"[REMINDFOR DENIED] user_id={from_user_id}")
            return
        if len(rest) < 2:
            send_telegram_message(chat_id, f"Usage: /remindfor <LABEL> [DATE] <TIME> [message]\nKnown labels: {', '.join(GROUP_LABELS.keys()) or 'none configured'}")
            return
        label = rest[0]
        target_chat_id = resolve_label(label)
        if not target_chat_id:
            send_telegram_message(chat_id, f"Unknown label '{label}'.")
            return
        remainder = rest[1:]
        maybe_date = parse_date_token(remainder[0], now_ist)
        if maybe_date is not None:
            target_date = maybe_date
            if len(remainder) < 2:
                send_telegram_message(chat_id, "Missing time.")
                return
            time_str = remainder[1]
            message_tokens = remainder[2:]
        else:
            target_date = today
            time_str = remainder[0]
            message_tokens = remainder[1:]
        message_text = " ".join(message_tokens) if message_tokens else "⏰ Reminder: check the market!"
        parsed_time = parse_time_to_24h(time_str)
        if parsed_time is None:
            send_telegram_message(chat_id, f"Couldn't understand time '{time_str}'.")
            return
        current_hm = now_ist.strftime("%H:%M")
        if target_date == today and parsed_time <= current_hm:
            send_telegram_message(chat_id, f"'{parsed_time}' has already passed today.")
            return
        if target_date < today:
            send_telegram_message(chat_id, f"'{target_date}' is in the past.")
            return
        add_reminder(target_chat_id, parsed_time, target_date, message_text)
        display_date = format_date_for_display(target_date, now_ist)
        send_telegram_message(chat_id, f"✅ Reminder set for {label} at {parsed_time} IST on {display_date}: \"{message_text}\"")
        print(f"[REMINDFOR] label={label} chat_id={target_chat_id} {parsed_time} {target_date} by user_id={from_user_id}")

    elif command == "/help":
        send_telegram_message(
            chat_id,
            "Stock commands:\n"
            "/addstock <SYMBOL> <PRICE> <above|below>\n"
            "/removestock <SYMBOL> [above|below] [PRICE]\n"
            "/liststocks\n\n"
            "Symbol format:\n"
            "NSE: TICKER.NS | BSE: TICKER.BO (ticker symbol, not numeric scrip code)\n\n"
            "Reminder commands (one-time):\n"
            "/remindme [DATE] <TIME> [message] - DATE optional (today/tomorrow/DD-MM-YYYY)\n"
            "/removereminder [DATE] <TIME>\n"
            "/listreminders [DATE]\n\n"
            "Market hours: 9:00 AM-3:30 PM IST, Mon-Fri.\n"
            "Your data here is independent from any other group.\n\n"
            "Tip: reply directly to any message from this bot and your reply will reach the admin."
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
            text = message.get("text") or message.get("caption") or ""
            chat_id = message.get("chat", {}).get("id")
            from_user_id = message.get("from", {}).get("id")
            photo_list = message.get("photo")
            photo_file_id = photo_list[-1]["file_id"] if photo_list else None

            if str(chat_id) not in ALLOWED_CHAT_IDS:
                continue

            reply_to = message.get("reply_to_message")
            is_reply_to_bot = bool(reply_to and reply_to.get("from", {}).get("is_bot"))

            if is_reply_to_bot and not text.startswith("/"):
                relay_reply_to_admin(chat_id, message)
                continue

            if text.startswith("/"):
                handle_command(text, chat_id, from_user_id, photo_file_id)
        time.sleep(COMMAND_POLL_INTERVAL_SECONDS)


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


def reminder_loop():
    print("Starting reminder checker...")
    while True:
        try:
            now = datetime.now(IST)
            current_hm = now.strftime("%H:%M")
            today = now.strftime("%Y-%m-%d")
            for chat_id, remind_time, message in load_due_reminders(today, current_hm):
                send_telegram_message(chat_id, message)
                delete_reminder_exact(chat_id, remind_time, today)
                print(f"[REMINDER SENT] chat={chat_id} at {remind_time} on {today}")
        except Exception as e:
            print(f"[REMINDER ERROR] {e}")
        time.sleep(REMINDER_CHECK_INTERVAL_SECONDS)


def main():
    if not ALLOWED_CHAT_IDS:
        print("[FATAL] No ALLOWED_CHAT_IDS configured.")
        return
    required = ["PGHOST", "PGDATABASE", "PGUSER", "PGPASSWORD"]
    missing = [v for v in required if not os.getenv(v)]
    if missing:
        print(f"[FATAL] Missing database env vars: {missing}")
        return
    init_db()
    print(f"Configured for {len(ALLOWED_CHAT_IDS)} chat(s): {ALLOWED_CHAT_IDS}")
    print(f"Broadcast targets: {BROADCAST_CHAT_IDS}")
    print(f"Group labels: {GROUP_LABELS}")
    print(f"Admin user configured: {'yes' if ADMIN_TELEGRAM_USER_ID else 'NO'}")
    print(f"Admin chat (reply relay target) configured: {'yes' if ADMIN_CHAT_ID else 'NO - reply relay disabled'}")
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