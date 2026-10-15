"""User choices made in Telegram (Save / Hide buttons, commands), persisted in state/prefs.json.
GitHub Actions has no always-on server, so taps and commands are read with getUpdates at the
start of each run (Telegram keeps them for 24 h)."""
import copy
import hashlib
import json
import os

from . import telegram

PATH = os.path.join(os.path.dirname(__file__), "..", "state", "prefs.json")

HELP = (
    "<b>PisoBot</b>\n"
    "Buttons: ❤️ Save · 👎 Hide · 🔕 Hide building. "
    "Buttons are checked every 5 minutes (GitHub may delay runs). "
    "Saved/hidden status appears on the card once processed.\n\n"
    "🆕 New since the last report · ❤️ Saved · ⭐ Deposit at most £10k.\n"
    "M = modelled mortgage · R = rent · SC = service charge. "
    "~ / est. means an estimated input, not a published cost. "
    "Mortgage uses the configured repayment rate/term, not a lender's offer.\n"
    "⚠️ High SC: over £250/mo. Lease under 90 years: confirm extension costs with a solicitor.\n"
    "Crime: Low/Med/High are thirds of the latest month's sampled London search locations "
    "(minimum 20 distinct ~100m locations), not official London-wide percentiles "
    "or population-adjusted risk. Unrated means there is insufficient reference data.\n"
    "Header: A = Oxford Circus; B = source listing pin (may be approximate). "
    "North-up straight line, not a transit route. Map: © OpenStreetMap contributors.\n"
    "Automated zero-new reports send a silent summary without unchanged cards. "
    "Every scheduled check can catch up a missed 06:30/18:00 UK report; GitHub may still delay it.\n\n"
    "/report – request a full report at the next button check\n"
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
    updates = telegram.get_updates(p["offset"])
    keyboards = {}
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
                if key in p["hidden"]:
                    p["hidden"].remove(key)
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
            message = cb["message"]
            message_key = (message["chat"]["id"], message["message_id"])
            keyboard = keyboards.setdefault(message_key, copy.deepcopy(message.get("reply_markup", {})))
            for row in keyboard.get("inline_keyboard", []):
                for button in row:
                    button_action, _, button_id = button.get("callback_data", "").partition(":")
                    if button_id == short and button_action in ("s", "h", "d"):
                        button["text"] = label if button_action == action else {
                            "s": "❤️ Save", "h": "👎 Hide", "d": "🔕 Hide building"
                        }[button_action]
            telegram.mark_message(*message_key, keyboard)
            print(f"pref: {label} {key}")
        elif msg and str(msg["chat"]["id"]) == chat:
            cmd = (msg.get("text") or "").split()[0].split("@")[0].lower() if msg.get("text") else ""
            if cmd == "/report":
                p["report_requested"] = True
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
