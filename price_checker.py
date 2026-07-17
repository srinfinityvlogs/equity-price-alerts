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
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

WATCHLIST_FILE = Path("watchlist.json")
ALERT_STATE_FILE = Path("alert_state.json")
OFFSET_FILE = Path("update_offset.json")

CHECK_INTERVAL_SECONDS = 180
COMMAND_POLL_INTERVAL_SECONDS = 5

IST = ZoneInfo("Asia/Kolkata")
MARKET_OPEN = dtime(9, 0)
MARKET_CLOSE = dtime(15, 30)


# ---------- Watchlist & alert state ----------

def load_watchlist():
    if not WATCHLIST_FILE.exists():
        return []
    with open(WATCHLIST_FILE, "r") as f:
        return json.load(f)


def save_watchlist(watchlist):
    with open(WATCHLIST_FILE, "w") as f:
        json.dump(watchlist, f, indent=2)


def load_alert_state():
    if ALERT_STATE_FILE.exists():
        try:
            with open(ALERT_STATE_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError):
            return {}
    return {}


def save_alert_state(state):
    with open(ALERT_STATE_FILE, "w") as f:
        json.dump(state, f)


def is_market_open():
    now = datetime.now(IST)
    if now.weekday() >= 5:  # Sat/Sun
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


def send_telegram_message(message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(url, data={"chat_id": CHAT_ID, "text": message}, timeout=10)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"[SEND ERROR] Failed to send message: {e}")


def check_watchlist():
    watchlist = load_watchlist()
    alert_state = load_alert_state()
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
            print(f"[WARN] No price data for {symbol}")
            continue

        alert_key = f"{symbol}:{condition}:{target}:{today}"
        already_alerted = alert_state.get(alert_key, False)

        triggered = (
            (condition == "above" and price > target) or
            (condition == "below" and price < target)
        )

        print(f"{symbol}: ₹{price:.2f} (target {condition} ₹{target}) — triggered={triggered}, already_alerted={already_alerted}")

        if triggered and not already_alerted:
            direction = "risen above" if condition == "above" else "fallen below"
            message = (
                f"📈 Price Alert: {display_name}\n"
                f"Current: ₹{price:.2f}\n"
                f"Has {direction} your target of ₹{target}"
            )
            send_telegram_message(message)
            alert_state[alert_key] = True
            save_alert_state(alert_state)
            print(f"[ALERTED] {symbol} ({condition} {target})")


# ---------- Chat command handling ----------

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
    if str(chat_id) != str(CHAT_ID):
        return

    parts = text.strip().split()
    if not parts:
        return

    command = parts[0].lower()

    if command == "/addstock":
        if len(parts) != 4:
            send_telegram_message(
                "Usage: /addstock <SYMBOL> <TARGET_PRICE> <above|below>\n"
                "Example: /addstock RELIANCE.NS 3000 above\n"
                "Tip: you can add a stock twice with different conditions "
                "(e.g. once 'above' and once 'below') to track two targets."
            )
            return

        symbol, price_str, condition = parts[1].upper(), parts[2], parts[3].lower()

        if condition not in ("above", "below"):
            send_telegram_message("Condition must be 'above' or 'below'.")
            return

        try:
            target_price = float(price_str)
        except ValueError:
            send_telegram_message(f"'{price_str}' is not a valid number.")
            return

        send_telegram_message(f"Checking if {symbol} is a valid symbol...")
        if not is_valid_symbol(symbol):
            send_telegram_message(
                f"Couldn't find price data for '{symbol}'. "
                f"Make sure it's a valid Yahoo Finance ticker (NSE stocks need '.NS', e.g. TCS.NS)."
            )
            return

        watchlist = load_watchlist()
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
        save_watchlist(watchlist)
        send_telegram_message(f"✅ Added {symbol}: alert when price goes {condition} ₹{target_price}")

    elif command == "/removestock":
        if len(parts) not in (2, 3):
            send_telegram_message(
                "Usage: /removestock <SYMBOL> [above|below]\n"
                "Omit above/below to remove ALL targets for that stock."
            )
            return

        symbol = parts[1].upper()
        condition_filter = parts[2].lower() if len(parts) == 3 else None

        if condition_filter and condition_filter not in ("above", "below"):
            send_telegram_message("Condition must be 'above' or 'below'.")
            return

        watchlist = load_watchlist()
        if condition_filter:
            new_watchlist = [
                s for s in watchlist
                if not (s["symbol"] == symbol and s["condition"] == condition_filter)
            ]
        else:
            new_watchlist = [s for s in watchlist if s["symbol"] != symbol]

        if len(new_watchlist) == len(watchlist):
            send_telegram_message(f"No matching entry found for '{symbol}'.")
        else:
            save_watchlist(new_watchlist)
            send_telegram_message(f"✅ Removed {symbol}{' (' + condition_filter + ')' if condition_filter else ''} from the watchlist.")

    elif command == "/liststocks":
        watchlist = load_watchlist()
        if not watchlist:
            send_telegram_message("Watchlist is empty.")
            return
        lines = ["📋 Current watchlist:"]
        for s in watchlist:
            status = "" if s.get("enabled", True) else " (disabled)"
            lines.append(f"- {s['symbol']}: {s['condition']} ₹{s['target_price']}{status}")
        send_telegram_message("\n".join(lines))

    elif command == "/help":
        send_telegram_message(
            "Available commands:\n"
            "/addstock <SYMBOL> <PRICE> <above|below>\n"
            "/removestock <SYMBOL> [above|below]\n"
            "/liststocks\n\n"
            "A stock can have two independent targets — one 'above' and one "
            "'below' — just add it twice with different conditions.\n\n"
            "Note: price checks only run during NSE market hours "
            "(9:00 AM-3:30 PM IST, Mon-Fri), but you can add/remove stocks anytime."
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
                check_watchlist()
            else:
                print("[MARKET CLOSED] Skipping check cycle")
        except Exception as e:
            print(f"[UNEXPECTED ERROR] {e} — continuing after short pause")
            time.sleep(10)
            continue
        time.sleep(CHECK_INTERVAL_SECONDS)


# ---------- Run both loops concurrently (daemon threads, clean Ctrl+C) ----------

def main():
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