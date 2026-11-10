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
import re

import requests

from . import d1, net, status as reports, users

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
    "The first scan establishes a baseline without alerts. Later scans send only "
    "new-home and price-drop alerts; there are no scheduled digests.\n\n"
    "/report – full rental digest now\n"
    "/rentals – fresh rental scan (new-home and price-drop alerts only)\n"
    "/saved – list saved homes\n"
    "/unhide – show hidden homes again\n"
    "/restart – clear hidden homes & run a fresh report\n"
    "/reset – clear saved, hidden and feedback preferences (keeps listing history)\n"
    "/settings – your search parameters\n"
    "/status – last scan, source health, deep-sweep coverage\n"
    "/whatif – what loosening rent, commute or crime would add (last 7 days)\n"
    "/set &lt;name&gt; &lt;value&gt; – change one, e.g. <code>/set rent 1500</code>, "
    "<code>/set destination Canary Wharf</code>, <code>/set commute default</code>\n"
    "/help – this message"
)
SETTINGS_HELP = (
    "\n\n<b>Money</b>: <b>rent</b> is the hard limit on the advertised monthly rent. "
    "<b>bills</b> only estimates utilities (energy, water, broadband); council tax is estimated "
    "from the advert's band and the borough's 2026-27 rate (London average, band C, when unknown). "
    "Cards show rent first, then the all-in figure (rent + bills). <b>budget</b> is an optional "
    "cap on that all-in figure: off unless you set it (<code>/set budget off</code> turns it off).\n"
    "Change with <code>/set &lt;name&gt; &lt;value&gt;</code>, e.g. <code>/set rent 1500</code>, "
    "<code>/set beds 2</code>, <code>/set commute 45</code>, <code>/set destination EC2M 7PY</code> "
    "(a postcode, station or place). <code>/set &lt;name&gt; default</code> restores one; "
    "<code>/set all default</code> restores everything.\n"
    "Each person's search is separate. After a change the next scan quietly re-records your "
    "current matches (no flood of alerts); /report shows them all."
)
WELCOME = (
    "👋 <b>Welcome to PisoBot</b>\n"
    "You have your own rental search: its own parameters, saved/hidden homes and alerts. "
    "Your first scan records current matches without alerting; then you get new homes and "
    "price drops as they appear. Start by checking /settings."
)
GEOCODE_TIMEOUT = 10


def _locate(query):
    """(name, lat, lng) for a London postcode, outcode, 'lat,lng', or station/place name."""
    query = " ".join(query.split())[:80]
    point = re.fullmatch(r"(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)", query)
    if point:
        lat, lng = float(point[1]), float(point[2])
        return f"{lat:.4f}, {lng:.4f}", lat, lng
    with requests.Session() as session:
        def get(url, **params):
            try:
                response = session.get(url, params=params, timeout=GEOCODE_TIMEOUT, verify=net.VERIFY)
                if response.status_code == 404:
                    return {}
                response.raise_for_status()
                result = response.json()
            except (requests.RequestException, ValueError):
                raise ValueError("Location lookup is unavailable right now; try again later") from None
            if not isinstance(result, dict):
                raise ValueError("Location lookup returned an invalid response; try again later")
            return result
        compact = query.upper().replace(" ", "")
        if re.fullmatch(r"[A-Z]{1,2}\d[A-Z\d]?\d[A-Z]{2}", compact):
            result = get(f"https://api.postcodes.io/postcodes/{compact}").get("result") or {}
            if result:
                return result["postcode"], result["latitude"], result["longitude"]
        elif re.fullmatch(r"[A-Z]{1,2}\d[A-Z\d]?", compact):
            result = get(f"https://api.postcodes.io/outcodes/{compact}").get("result") or {}
            if result:
                return result["outcode"], result["latitude"], result["longitude"]
        params = {"modes": "tube,elizabeth-line,overground,dlr,national-rail", "maxResults": 1}
        if os.environ.get("TFL_APP_KEY"):
            params["app_key"] = os.environ["TFL_APP_KEY"]
        matches = get(f"https://api.tfl.gov.uk/StopPoint/Search/{requests.utils.quote(query)}",
                      **params).get("matches") or []
        if matches and "lat" in matches[0] and "lon" in matches[0]:
            name = re.sub(r" (?:Underground|Rail|DLR) Station$| Station$", "", matches[0].get("name") or query)
            return name, matches[0]["lat"], matches[0]["lon"]
    raise ValueError(f"Couldn't find {html.escape(query)}; try a full postcode such as EC2M 7PY")


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
        self.chat_id = str(chat_id)        # the owner's chat
        self.request_run = request_run
        self.client = client
        self.user = None                   # who the update being handled belongs to

    @property
    def chat(self):
        return self.user.chat_id if self.user is not None and self.user.chat_id else self.chat_id

    def reply(self, text, chat_id=None):
        self.client.call("sendMessage", chat_id=chat_id or self.chat, text=text,
                         parse_mode="HTML", disable_web_page_preview=True)

    def _person(self, chat, message=None):
        """Owner, an admitted person, or (for an invited username's first private /start) a
        newly admitted person; None for everyone else."""
        if _own_chat(chat, self.chat_id):
            return users.owner() if users.enabled() else users.User(self.chat_id, owner=True)
        if (not users.enabled() or not isinstance(chat, dict)
                or type(chat.get("id")) not in (str, int) or not str(chat["id"]).lstrip("-").isdigit()):
            return None
        person = users.get(chat["id"])
        if person is not None or message is None or chat.get("type") != "private":
            return person
        words = (message.get("text") or "").split()
        if not words or words[0].split("@")[0].lower() != "/start":
            return None
        sender = message.get("from") if isinstance(message.get("from"), dict) else {}
        username = sender.get("username") if isinstance(sender.get("username"), str) else ""
        if not username or username.lower() not in users.invited():
            return None
        name = sender.get("first_name") if isinstance(sender.get("first_name"), str) else username
        person = users.register(chat["id"], name[:40], username)
        if person is not None:
            LOG.info("Admitted a new PisoBot user")
            self.reply(WELCOME, person.chat_id)
            try:
                self.reply(f"👥 {html.escape(person.label)} (@{html.escape(username)}) joined PisoBot "
                           "with their own search.", self.chat_id)
            except TelegramError:
                LOG.warning("Owner notification failed")
        return person

    def handle(self, update):
        if not isinstance(update, dict):
            return
        query = update.get("callback_query")
        if query is not None:
            if not isinstance(query, dict):
                return
            message = query.get("message")
            if not isinstance(message, dict):
                return
            person = self._person(message.get("chat"))
            if person is None:
                return
            with users.use(person):
                self.user = person
                try:
                    self.callback(query, message)
                finally:
                    self.user = None
            return
        message = update.get("message")
        if isinstance(message, dict) and isinstance(message.get("text"), str):
            person = self._person(message.get("chat"), message)
            if person is None:
                return
            with users.use(person):
                self.user = person
                try:
                    self.command(message["text"])
                finally:
                    self.user = None

    def _request(self, kind):
        if self.user is None or self.user.owner:
            return self.request_run(kind)
        return self.request_run(kind, self.user.chat_id)

    def settings(self, words):
        person = self.user or users.User(self.chat_id, owner=True)
        if not users.enabled():
            self.reply("Search settings can only be changed on the VPS bot.")
            return
        if len(words) < 2:
            self.reply(f"<b>🔎 Your search</b>\n{users.describe(person)}{SETTINGS_HELP}")
            return
        name, raw = words[1].lower(), " ".join(words[2:]).strip()
        params = dict(person.params)
        if name == "all" and raw.lower() == "default":
            params = {}
        elif name not in (*users.PARAMS, "destination") or not raw:
            self.reply("Usage: <code>/set &lt;name&gt; &lt;value&gt;</code> with one of "
                       + ", ".join(f"<code>{n}</code>" for n in (*users.PARAMS, "destination"))
                       + ". See /settings.")
            return
        elif raw.lower() == "default" or (name == "budget" and raw.lower() in ("off", "none", "no")):
            params.pop(name, None)   # budget's default is off: no all-in cap
        else:
            try:
                if name == "destination":
                    label, lat, lng = _locate(raw)
                    if any(type(value) not in (int, float) or not math.isfinite(value) for value in (lat, lng)):
                        raise ValueError("Location lookup returned invalid coordinates; try again later")
                    if not users.in_london(lat, lng):
                        raise ValueError(f"{html.escape(label)} is outside London")
                    params[name] = {"name": label, "lat": round(lat, 5), "lng": round(lng, 5)}
                else:
                    params[name] = users.parse(name, raw)
            except ValueError as error:
                self.reply(f"⚠️ {error}")
                return
        if params == person.params:
            self.reply("No change.\n" + users.describe(person))
            return
        users.save_params(person, params)
        # The next scan re-records this person's matches under the new criteria instead of
        # alerting every newly matching home as "new".
        d1.query("DELETE FROM meta WHERE name = 'watch_initialized'")
        accepted = self._request("rentals")
        status = ("A scan has been requested." if accepted
                  else "A scan is already queued or running. Your settings apply on the next scan.")
        self.reply(f"✅ Search updated. {status}\n\n<b>🔎 Your search</b>\n{users.describe(person)}")

    def people(self, words):
        if self.user is not None and not self.user.owner:
            return
        if not users.enabled():
            self.reply("Sharing is only available on the VPS bot.")
            return
        if words[0] == "/removeuser" and len(words) == 2:
            try:
                users.remove(words[1])
            except ValueError as error:
                self.reply(str(error))
                return
            self.reply("Access revoked (their settings and history stay on disk).")
            return
        lines = [f"• {html.escape(p.label)}{' (you)' if p.owner else ''} · <code>{html.escape(p.chat_id)}</code>"
                 for p in users.everyone()]
        invites = ", ".join("@" + html.escape(u) for u in sorted(users.invited())) or "none"
        self.reply("<b>👥 PisoBot users</b>\n" + "\n".join(lines)
                   + f"\nInvited usernames: {invites} · max {users.max_users()}"
                   + "\n<code>/removeuser &lt;chat_id&gt;</code> removes someone.")

    def command(self, text):
        words = text.split()
        command = words[0].split("@")[0].lower() if words else ""
        if words:
            words[0] = command
        if command in ("/settings", "/search", "/set"):
            self.settings(words if command == "/set" else words[:1])
            return
        if command in ("/status", "/whatif"):
            if not users.enabled():
                self.reply("This command is only available on the VPS bot.")
            elif command == "/status":
                self.reply(reports.status_text())
            else:
                self.reply(reports.whatif_text(self.user or users.User(self.chat_id, owner=True)))
            return
        if command in ("/users", "/removeuser"):
            self.people(words)
            return
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
            accepted = self._request(kind)
            prefix = {"/restart": "🔄 Hidden homes cleared.\n",
                      "/reset": "💥 Saved homes, hidden homes and taste feedback cleared. "
                                "Listing history and scheduler state kept.\n"}.get(command, "")
            if kind == "rentals":
                status = ("🔎 Rental scan requested · only new-home and price-drop alerts "
                          "will be sent; no full digest."
                          if accepted else "🔎 Rental scan already queued or running; no full digest.")
            else:
                status = ("📊 Full rental digest requested · it arrives in a few minutes."
                          if accepted else "📊 Another scan is queued or running. "
                          "Try /report again after it finishes.")
            self.reply(prefix + status)
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
