import os
import sys
import requests
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
BROADCAST_CHAT_IDS = [c.strip() for c in os.getenv("BROADCAST_CHAT_IDS", "").split(",") if c.strip()]


def send_message(chat_id, text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    resp = requests.post(url, data={"chat_id": chat_id, "text": text}, timeout=10)
    return resp.status_code == 200


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 broadcast.py \"Your message here\"")
        sys.exit(1)

    message = sys.argv[1]

    if not BROADCAST_CHAT_IDS:
        print("No BROADCAST_CHAT_IDS configured in .env")
        sys.exit(1)

    print(f"Broadcasting to {len(BROADCAST_CHAT_IDS)} chat(s):")
    for chat_id in BROADCAST_CHAT_IDS:
        success = send_message(chat_id, message)
        status = "✅ sent" if success else "❌ failed"
        print(f"  {chat_id}: {status}")