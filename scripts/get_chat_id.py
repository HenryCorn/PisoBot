"""Send any message to your bot in Telegram, then run:
    TELEGRAM_BOT_TOKEN=... python scripts/get_chat_id.py
and put the printed id in the TELEGRAM_CHAT_ID secret."""
import os

import requests

r = requests.get(f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/getUpdates", timeout=30).json()
chats = {(u.get("message") or {}).get("chat", {}).get("id"): (u.get("message") or {}).get("chat", {}).get("first_name")
         for u in r.get("result", [])}
print(chats or "No messages yet - send /start to the bot first.")
