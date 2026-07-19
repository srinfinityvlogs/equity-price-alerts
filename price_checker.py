import os
import json
import time
import threading
import requests
import yfinance as yf
from pathlib import Path
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ALLOWED_CHAT_IDS = [c.strip() for c in os.getenv("ALLOWED_CHAT_IDS", "").split(",") if c.strip()]

OFFSET_FILE = Path("update_offset.json")

CHECK_INTERVAL_SECONDS = 180
COMMAND_POLL_INTERVAL_SECONDS = 5

IST = ZoneInfo("Asia/Kolkata")
MARKET_OPEN = dtime(9, 0)
MARKET_CLOSE = dtime(15, 30)


def watchlist_file(chat_id):
    return Path(f"watchlist_{chat_id}.json")


def alert_state_file(chat_id):
    return Path(f"alert_state_{chat_id}.json")


def load_watchlist(chat_id):
    f = watchlist_file(chat_id)
    if not f.exists():
        return []
    with open(f, "r") as fh:
        return json.load(fh)


def save_watchlist(chat_id, watchlist):
    with open(watchlist_file(chat_id), "w") as fh:
        json.dump(watchlist, fh, indent=2)


def load_alert_state(chat_id):
    f = alert_state_file(chat_id)
    if f.exists():
        try:
            with open(f, "r") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, ValueError):
            return {}
    return {}


def save_alert_state(chat_id, state):
    with open(alert_state_file(chat_id), "w") as fh:
        json.dump(state, fh)


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
    alert_state = load_alert_state(chat_id)
    today = get_today_key()

    for stock in watchlist:
        if not stock.get("enabled", True):
            continue

        symbol = stock["symbol"]
        target = stock["target_price"]
        condition = stock["condition"]
        display_name = stock.get("display_name", symbol)

        price = fetch_price(symbol)
        if price is None:
            print(f"[WARN] chat={chat_id}: No price data for {symbol}")
            continue

        alert_key = f"{symbol}:{condition}:{target}:{today}"
        already_alerted = alert_state.get(alert_key, False)

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
            alert_state[alert_key] = True
            save_alert_state(chat_id, alert_state)
            print(f"[ALERTED] chat={chat_id} {symbol} ({condition} {target})")


def load_offset():
    if OFFSET_FILE.exists():
        try:
            with open(OFFSET_FILE, "r") as f:
                return json.load(f).get("offset", 0)
        except (json.JSONDecodeError, ValueError):
            return 0
    return 0


def save_offset(offset):
    with open(OFFSET_FILE, "w") as f:
        json.dump({"offset": offset}, f)


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
                "Example: /addstock RELIANCE.NS 3000 above"
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

        watchlist = load_watchlist(chat_id)
        watchlist = [
            s for s in watchlist
            if not (s["symbol"] == symbol and s["condition"] == condition)
        ]
        watchlist.append({
            "symbol": symbol,
            "display_name": symbol.replace(".NS", "").replace(".BO", ""),
            "target_price": target_price,
            "condition": condition,
            "enabled": True
        })
        save_watchlist(chat_id, watchlist)
        send_telegram_message(chat_id, f"✅ Added {symbol}: alert when price goes {condition} ₹{target_price}")

    elif command == "/removestock":
        if len(parts) not in (2, 3):
            send_telegram_message(
                chat_id,
                "Usage: /removestock <SYMBOL> [above|below]"
            )
            return

        symbol = parts[1].upper()
        condition_filter = parts[2].lower() if len(parts) == 3 else None

        if condition_filter and condition_filter not in ("above", "below"):
            send_telegram_message(chat_id, "Condition must be 'above' or 'below'.")
            return

        watchlist = load_watchlist(chat_id)
        if condition_filter:
            new_watchlist = [
                s for s in watchlist
                if not (s["symbol"] == symbol and s["condition"] == condition_filter)
            ]
        else:
            new_watchlist = [s for s in watchlist if s["symbol"] != symbol]

        if len(new_watchlist) == len(watchlist):
            send_telegram_message(chat_id, f"No matching entry found for '{symbol}'.")
        else:
            save_watchlist(chat_id, new_watchlist)
            send_telegram_message(chat_id, f"✅ Removed {symbol}")

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
            "/removestock <SYMBOL> [above|below]\n"
            "/liststocks\n\n"
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


def main():
    if not ALLOWED_CHAT_IDS:
        print("[FATAL] No ALLOWED_CHAT_IDS configured. Set it in .env, comma-separated.")
        return

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
