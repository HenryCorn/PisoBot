"""Telegram long polling for the VPS runner.

``poll(request_run, stop_event)`` blocks until the threading.Event is set.
``request_run("report" | "rentals")`` must enqueue work without blocking and
return True if accepted, False if already queued/running. The watch runner owns
serialization; this module never starts a report process or imports watch.

Only one poller may run for a bot. Startup removes and verifies the webhook,
without dropping pending updates. D1 meta stores the next update offset.
Delivery is at least once: a failed handler/checkpoint is retried, so Telegram
replies and run requests may repeat after a failure (the trigger must coalesce).
Preference resets preserve listing history and watch scheduler metadata.
"""
import copy
import datetime as dt
import html
import json
import logging
import math
import os

import requests

from . import d1, net

LOG = logging.getLogger(__name__)
OFFSET_KEY = "telegram_update_offset"
HTTP_TIMEOUT = 35
POLL_TIMEOUT = 25
RETRY_SECONDS = 3
DEFAULT_LABELS = {"s": "❤️ Save", "h": "👎 Hide", "d": "🔕 Hide building"}
ACTIVE_LABELS = {"s": "🩶 Saved", "h": "🩶 Hidden", "d": "🔕 Building hidden"}
REASON_LABELS = {
    "area": "📍 Area", "value": "💷 Value", "vibe": "🌿 Vibe",
    "commute": "🚇 Commute", "price": "💷 Price", "trap": "⚠️ Red Flag",
}
REASONS = {"s": ("area", "value", "vibe", "commute"),
           "h": ("price", "commute", "area", "trap")}
HELP = (
    "<b>PisoBot</b>\n"
    "Buttons: ❤️ Save · 👎 Hide · 🔕 Hide building. They apply instantly; "
    "hidden homes drop out of the next report.\n"
    "Save un-hides a home; Hide un-saves it. Tap an optional reason chip to "
    "train your recommendation taste. /unhide undoes hidden homes/buildings.\n\n"
    "🆕 New since the last report · 🩶 Saved · ⭐ Deposit at most £10k.\n"
    "M = modelled mortgage · R = rent · SC = service charge. "
    "~ / est. means an estimated input, not a published cost. "
    "Mortgage uses the configured repayment rate/term, not a lender's offer.\n"
    "⚠️ High SC: over £250/mo. Lease under 90 years: confirm extension costs with a solicitor.\n"
    "Crime: Low/Med/High are thirds of the latest month's sampled London search locations "
    "(minimum 20 distinct ~100m locations), not official London-wide percentiles "
    "or population-adjusted risk. Unrated means there is insufficient reference data.\n"
    "Header: A = Oxford Circus; B = source listing pin (may be approximate). "
    "North-up straight line, not a transit route. Map: © OpenStreetMap contributors.\n"
    "Reports arrive at 06:30 and 18:00 UK time; zero-new reports are a silent summary.\n\n"
    "/report – full report now\n"
    "/rentals – private rentals report\n"
    "/saved – list saved homes\n"
    "/unhide – show hidden homes again\n"
    "/restart – clear hidden homes & run a fresh report\n"
    "/reset – clear saved, hidden and feedback preferences (keeps listing history)\n"
    "/help – this message"
)


class TelegramError(RuntimeError):
    """A deliberately token-free Telegram failure."""

    def __init__(self, message, unavailable_message=False):
        super().__init__(message)
        self.unavailable_message = unavailable_message


class TelegramClient:
    def __init__(self, token, session):
        self._token = token
        self.session = session

    def call(self, method, **body):
        try:
            response = self.session.post(
                f"https://api.telegram.org/bot{self._token}/{method}",
                json=body, timeout=HTTP_TIMEOUT, verify=net.VERIFY,
            )
            result = response.json()
        except Exception:
            # Requests exceptions include the full token-bearing URL.
            raise TelegramError("Telegram request failed") from None
        if (not 200 <= response.status_code < 300 or not isinstance(result, dict)
                or result.get("ok") is not True):
            description = result.get("description", "") if isinstance(result, dict) else ""
            edit_error = (method == "editMessageReplyMarkup" and isinstance(result, dict)
                          and result.get("error_code") == 400 and isinstance(description, str))
            if edit_error and description.startswith("Bad Request: message is not modified"):
                return True
            unavailable = edit_error and description in (
                "Bad Request: message to edit not found",
                "Bad Request: message can't be edited",
            )
            raise TelegramError("Telegram request failed", unavailable_message=unavailable)
        return result.get("result")

    def clear_webhook(self):
        if self.call("deleteWebhook") is not True:
            raise TelegramError("Unable to remove Telegram webhook")
        info = self.call("getWebhookInfo")
        if not isinstance(info, dict) or info.get("url") != "":
            raise TelegramError("Telegram webhook is not confirmed cleared")


def _own_chat(chat, chat_id):
    return (isinstance(chat, dict) and type(chat.get("id")) in (str, int)
            and str(chat["id"]) == chat_id)


def _reason_button(button, sid):
    data = button.get("callback_data", "")
    return isinstance(data, str) and data.startswith("r:") and data.endswith(":" + sid)


def _reason_row(action, sid):
    return [{"text": REASON_LABELS[r], "callback_data": f"r:{action}:{r}:{sid}"}
            for r in REASONS.get(action, ())]


def _keyboard(message):
    markup = message.get("reply_markup", {"inline_keyboard": []})
    if not isinstance(markup, dict):
        return None
    rows = markup.get("inline_keyboard", [])
    if not isinstance(rows, list) or any(
        not isinstance(row, list) or any(not isinstance(b, dict) for b in row)
        for row in rows
    ):
        return None
    return copy.deepcopy(markup)


def _update_keyboard(markup, action, sid, note=None):
    rows, output, inserted = markup.get("inline_keyboard", []), [], False
    has_existing_reason = any(_reason_button(b, sid) for row in rows for b in row)
    for row in rows:
        # Remove only this home's reason chips, never unrelated buttons in their row.
        has_reason = any(_reason_button(b, sid) for b in row)
        remaining = [b for b in row if not _reason_button(b, sid)]
        has_action = False
        for button in remaining:
            data = button.get("callback_data")
            for candidate in DEFAULT_LABELS:
                if data == f"{candidate}:{sid}":
                    has_action = True
                    if note is None:
                        button["text"] = (ACTIVE_LABELS[action] if candidate == action
                                          else DEFAULT_LABELS[candidate])
        if remaining:
            output.append(remaining)
        if not inserted and (has_reason or (has_action and note is None and not has_existing_reason)):
            reasons = ([{"text": note, "callback_data": f"r:ack:{sid}"}] if note
                       else _reason_row(action, sid))
            if reasons:
                output.append(reasons)
                inserted = True
    markup["inline_keyboard"] = output
    return markup


class Handler:
    """Allowlisted update handler; client.call and request_run are injectable."""

    def __init__(self, chat_id, request_run, client):
        self.chat_id = str(chat_id)
        self.request_run = request_run
        self.client = client

    def reply(self, text):
        self.client.call("sendMessage", chat_id=self.chat_id, text=text,
                         parse_mode="HTML", disable_web_page_preview=True)

    def handle(self, update):
        if not isinstance(update, dict):
            return
        query = update.get("callback_query")
        if query is not None:
            if not isinstance(query, dict):
                return
            message = query.get("message")
            if (not isinstance(message, dict)
                    or not _own_chat(message.get("chat"), self.chat_id)):
                return
            self.callback(query, message)
            return
        message = update.get("message")
        if (isinstance(message, dict) and _own_chat(message.get("chat"), self.chat_id)
                and isinstance(message.get("text"), str)):
            self.command(message["text"])

    def command(self, text):
        words = text.split()
        command = words[0].split("@")[0].lower() if words else ""
        if command in ("/report", "/rentals", "/restart", "/reset"):
            if command in ("/restart", "/reset"):
                d1.query("DELETE FROM hidden")
                d1.query("DELETE FROM hidden_dev")
                if command == "/reset":
                    d1.query("DELETE FROM saved")
                    d1.query("DELETE FROM feedback")
                # Reset only preference-owned metadata, never scheduler/transport state.
                d1.query("DELETE FROM meta WHERE name = 'last_report'")
            kind = "rentals" if command == "/rentals" else "report"
            accepted = self.request_run(kind)
            prefix = {"/restart": "🔄 Hidden homes cleared.\n",
                      "/reset": "💥 Saved homes, hidden homes and taste feedback cleared. "
                                "Listing history and scheduler state kept.\n"}.get(command, "")
            self.reply(prefix + ("📊 Report requested · it arrives in a few minutes."
                                 if accepted else "📊 Report already queued or running."))
        elif command == "/saved":
            lines = []
            for row in d1.query("SELECT info FROM saved"):
                try:
                    info = json.loads(row["info"])
                    cost = float(info.get("cost") or 0)
                    if not math.isfinite(cost):
                        cost = 0
                    url = html.escape(str(info.get("url") or ""), quote=True)
                    address = html.escape(str(info.get("address") or "listing")[:60])
                    lines.append(f'❤️ £{math.floor(cost + 0.5):,} · <a href="{url}">{address}</a>')
                except (ValueError, TypeError, KeyError, AttributeError):
                    lines.append("❤️ Saved home details unavailable.")
            # Telegram has a 4096-character text limit; keep large saved lists usable.
            text = "<b>Saved homes</b>"
            for line in lines or ["Nothing saved yet."]:
                if len(text) + len(line) > 3800:
                    self.reply(text)
                    text = "<b>Saved homes (continued)</b>"
                text += "\n" + line
            self.reply(text)
        elif command == "/unhide":
            d1.query("DELETE FROM hidden")
            d1.query("DELETE FROM hidden_dev")
            self.reply("👀 Hidden homes will show again from the next report.")
        elif command in ("/help", "/start"):
            self.reply(HELP)

    def callback(self, query, message):
        data, query_id = query.get("data"), query.get("id")
        if (not isinstance(data, str) or not data or len(data.encode("utf-8", errors="replace")) > 64
                or not isinstance(query_id, str) or not query_id
                or type(message.get("message_id")) is not int):
            return
        markup = _keyboard(message)
        if markup is None:
            return
        parts = data.split(":")
        def answer(text=""):
            try:
                self.client.call("answerCallbackQuery", callback_query_id=query_id, text=text)
            except TelegramError:
                # Callback acknowledgements expire and never determine durable choices.
                LOG.warning("Telegram callback acknowledgement failed")

        if len(parts) == 3 and parts[:2] == ["r", "ack"] and parts[2]:
            answer("Preference already noted!")
            return
        reason = None
        if len(parts) == 4 and parts[0] == "r":
            _, action, reason, sid = parts
            if reason not in REASONS.get(action, ()):
                answer()
                return
        elif len(parts) == 2 and parts[0] in DEFAULT_LABELS:
            action, sid = parts
        else:
            answer()
            return
        if not sid:
            answer()
            return
        listings = d1.query("SELECT * FROM listings WHERE sid = ?", sid)
        if not listings:
            answer("Listing no longer tracked")
            return
        listing = listings[0]
        if reason:
            d1.query(
                "INSERT INTO feedback (key, action, reason, created_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(key, action, reason) DO NOTHING",
                listing["key"], action, reason, dt.datetime.now(dt.timezone.utc).isoformat(),
            )
            label = "✓ Noted: " + REASON_LABELS[reason]
            markup = _update_keyboard(markup, action, sid, label)
        else:
            if action == "s":
                info = {name: listing.get(name) for name in ("lat", "lng", "url", "address", "cost")}
                info["missing"] = 0
                d1.query("INSERT INTO saved (key, info) VALUES (?, ?) "
                         "ON CONFLICT(key) DO UPDATE SET info = excluded.info",
                         listing["key"], json.dumps(info))
                d1.query("DELETE FROM hidden WHERE key = ?", listing["key"])
            elif action == "h":
                d1.query("INSERT OR IGNORE INTO hidden (key) VALUES (?)", listing["key"])
                d1.query("DELETE FROM saved WHERE key = ?", listing["key"])
            else:
                coords = [listing.get(name) for name in ("lat", "lng")]
                if any(type(n) not in (int, float) or not math.isfinite(n) for n in coords):
                    answer("Building location unavailable")
                    return
                d1.query("INSERT OR IGNORE INTO hidden_dev (lat, lng) VALUES (?, ?)", *coords)
            label = ACTIVE_LABELS[action]
            markup = _update_keyboard(markup, action, sid)
        try:
            self.client.call("editMessageReplyMarkup", chat_id=message["chat"]["id"],
                             message_id=message["message_id"], reply_markup=markup)
        except TelegramError as error:
            if not error.unavailable_message:
                raise
            LOG.warning("Telegram keyboard unavailable; preference was recorded")
        answer(label)


def _offset():
    rows = d1.query("SELECT value FROM meta WHERE name = ?", OFFSET_KEY)
    if not rows:
        return 0
    try:
        value = int(rows[0]["value"])
        if value < 0:
            raise ValueError
        return value
    except (ValueError, TypeError, KeyError):
        raise RuntimeError("Invalid Telegram update checkpoint") from None


def poll(request_run, stop_event):
    """Run the allowlisted bot with an injected, nonblocking/coalescing run trigger."""
    if stop_event.is_set():
        return
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set")
    # Never borrow net.session: reports use it concurrently for scraping and D1.
    with requests.Session() as session:
        client = TelegramClient(token, session)
        client.clear_webhook()
        offset = _offset()
        handler = Handler(chat_id, request_run, client)
        while not stop_event.is_set():
            try:
                updates = client.call("getUpdates", offset=offset, timeout=POLL_TIMEOUT,
                                      allowed_updates=["message", "callback_query"])
                if not isinstance(updates, list):
                    raise TelegramError("Invalid Telegram update response")
                updates = sorted(
                    (u for u in updates if isinstance(u, dict)
                     and type(u.get("update_id")) is int and u["update_id"] >= offset),
                    key=lambda u: u["update_id"],
                )
                for update in updates:
                    if stop_event.is_set():
                        return
                    if update["update_id"] < offset:
                        continue
                    handler.handle(update)
                    next_offset = update["update_id"] + 1
                    d1.query("INSERT INTO meta (name, value) VALUES (?, ?) "
                             "ON CONFLICT(name) DO UPDATE SET value = excluded.value",
                             OFFSET_KEY, str(next_offset))
                    offset = next_offset
            except Exception:
                # Do not log exceptions/tracebacks: HTTP errors can disclose bot tokens.
                LOG.warning("Telegram polling/handling failed; retrying unacknowledged updates")
                stop_event.wait(RETRY_SECONDS)
