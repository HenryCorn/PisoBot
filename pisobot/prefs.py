"""User choices made in Telegram (Save / Hide buttons, commands), persisted in state/prefs.json.
GitHub Actions has no always-on server, so taps and commands are read with getUpdates at the
start of each run (Telegram keeps them for 24 h)."""
import hashlib
import json
import os

from . import telegram

PATH = os.path.join(os.path.dirname(__file__), "..", "state", "prefs.json")

HELP = (
    "<b>PisoBot</b>\n"
    "Buttons: ❤️ Save · 👎 Hide · 🔕 Hide building (individual alerts). "
    "They apply at the next run: 06:30 or 18:00 UK time.\n\n"
    "/report – full report (sent by the next run)\n"
    "/saved – list saved homes\n"
    "/unhide – show hidden homes again\n"
    "/help – this message"
)


def load():
    try:
        with open(PATH, encoding="utf-8") as f:
            p = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        p = {}
    p.setdefault("offset", 0)
    p.setdefault("saved", {})
    p.setdefault("hidden", [])
    p.setdefault("hidden_dev", [])
    p.setdefault("report_requested", False)
    return p


def save(p):
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    with open(PATH, "w", encoding="utf-8") as f:
        json.dump(p, f, indent=1, sort_keys=True)


def sid(key):
    return hashlib.sha1(key.encode()).hexdigest()[:10]


def is_hidden(p, l):
    if l.key in p["hidden"]:
        return True
    return any(abs(l.lat - lat) < 0.0015 and abs(l.lng - lng) < 0.0025 for lat, lng in p["hidden_dev"])


def process_updates(p, known):
    """Apply button taps and commands. known: {key: {"lat", "lng", "address", "url", "cost"}}."""
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    by_sid = {sid(k): k for k in known}
    try:
        updates = telegram.get_updates(p["offset"])
    except Exception as ex:
        print(f"getUpdates failed: {ex}")
        return
    for u in updates:
        p["offset"] = u["update_id"] + 1
        cb = u.get("callback_query")
        msg = u.get("message")
        if cb:
            if str(cb["message"]["chat"]["id"]) != chat:
                continue
            action, _, short = cb.get("data", "").partition(":")
            key = by_sid.get(short)
            if not key:
                telegram.answer_callback(cb["id"], "Listing no longer tracked")
                continue
            info = known[key]
            if action == "s":
                p["saved"][key] = {**info, "missing": 0}
                label = "❤️ Saved"
            elif action == "h":
                if key not in p["hidden"]:
                    p["hidden"].append(key)
                p["saved"].pop(key, None)
                label = "👎 Hidden"
            elif action == "d":
                p["hidden_dev"].append([info["lat"], info["lng"]])
                label = "🔕 Building hidden"
            else:
                continue
            telegram.answer_callback(cb["id"], label)
            keyboard = cb["message"].get("reply_markup", {}).get("inline_keyboard", [])
            ids = {button["callback_data"].partition(":")[2]
                   for row in keyboard for button in row if "callback_data" in button}
            if ids == {short}:
                telegram.mark_message(cb["message"]["chat"]["id"], cb["message"]["message_id"], label)
            print(f"pref: {label} {key}")
        elif msg and str(msg["chat"]["id"]) == chat:
            cmd = (msg.get("text") or "").split()[0].split("@")[0].lower() if msg.get("text") else ""
            if cmd == "/report":
                p["report_requested"] = True   # sent later in this same run
            elif cmd == "/saved":
                saved = p["saved"]
                lines = [f'❤️ £{v.get("cost", 0):,.0f} · <a href="{v["url"]}">{telegram.e(v["address"][:60])}</a>'
                         for v in saved.values()] or ["Nothing saved yet."]
                telegram.send_text("<b>Saved homes</b>\n" + "\n".join(lines))
            elif cmd == "/unhide":
                p["hidden"], p["hidden_dev"] = [], []
                telegram.send_text("👀 Hidden homes will show again from the next run.")
            elif cmd in ("/help", "/start"):
                telegram.send_text(HELP)
