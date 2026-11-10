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
import time

import requests

from . import archive, config, d1, net, renter, status as reports, tgtext, tracker, users

LOG = logging.getLogger(__name__)
OFFSET_KEY = "telegram_update_offset"
HTTP_TIMEOUT = 35
POLL_TIMEOUT = 25
RETRY_SECONDS = 3
TICK_SECONDS = 60      # viewing reminders and pipeline change notices are checked this often
NOTICES_PER_TICK = 10  # pipeline change notices per person per tick (reminders are never capped)
WP3_PREFIXES = ("e", "p", "P", "v", "q", "m", "o")
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
    "/pipeline – homes you are pursuing, by stage, with the next step (VPS)\n"
    "/profile – your renter profile for enquiry drafts and move-in fit (VPS)\n"
    "/onboard – set up your search and profile one question at a time (VPS)\n"
    "<code>/set urgent on</code> – ⚡ top matches ring even in quiet hours (VPS; default off)\n"
    "On rental cards (VPS): ✉️ Enquired starts tracking a home, ✍️ Enquiry text drafts a message "
    "for you to send yourself (PisoBot never contacts agents), 📷 More photos sends the rest.\n"
    "/cancel – stop waiting for a typed answer\n"
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

    def __init__(self, message, unavailable_message=False, permanent=False):
        super().__init__(message)
        self.unavailable_message = unavailable_message
        # Telegram answered 400/403: this exact request will never succeed (too long, malformed,
        # chat blocked...). Retrying it would only hold back every later update.
        self.permanent = permanent


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
            edit_error = (method in ("editMessageReplyMarkup", "editMessageText") and isinstance(result, dict)
                          and result.get("error_code") == 400 and isinstance(description, str))
            if edit_error and description.startswith("Bad Request: message is not modified"):
                return True
            unavailable = edit_error and description in (
                "Bad Request: message to edit not found",
                "Bad Request: message can't be edited",
            )
            code = result.get("error_code") if isinstance(result, dict) else None
            code = code if type(code) is int else response.status_code
            raise TelegramError("Telegram request failed", unavailable_message=unavailable,
                                permanent=code in (400, 403))
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


# WP3 onboarding: (setting, question, suggested answers). The first four write the same search
# settings as /set; the rest fill the renter profile. Every answer can also be typed.
ONBOARDING = (
    ("destination", "📍 Where do you commute to most days? Send a postcode, station or place, e.g. "
                    "<code>EC2M 7PY</code> or <code>Canary Wharf</code>.", ()),
    ("rent", "💷 Highest advertised monthly rent you would pay (before bills)? Tap one or type a number.",
     ("1200", "1500", "1800", "2100", "2500")),
    ("commute", "🚇 Longest commute, in minutes, door to door?", ("30", "40", "50", "60")),
    ("beds", "🛏️ Minimum bedrooms?", ("1", "2", "3")),
    ("movein", "📅 When do you want to move in? e.g. <code>1 Dec</code>, <code>1/12-15/12</code>, "
               "<code>December</code>, <code>late December</code> or <code>asap</code>.", ("asap",)),
    ("name", "👤 Your name, for enquiry drafts?", ()),
    ("job", "💼 Your occupation, e.g. <code>nurse, full time</code>?", ()),
    ("household", "🏠 Who will live there, e.g. <code>couple, no children</code>?", ()),
    ("pets", "🐾 Any pets? Tap or type, e.g. <code>one indoor cat</code>.", ("none",)),
    ("smoker", "🚬 Do you smoke?", ("no", "yes")),
    ("references", "📄 References you can provide?", ("employer and previous landlord", "not yet")),
)
SEARCH_STEPS = ("destination", "rent", "commute", "beds")


def _stage_button(data, sid):
    return isinstance(data, str) and (data == f"e:{sid}" or (
        data.startswith("p:") and data.endswith(":" + sid) and data.count(":") == 2))


def _stage_markup(markup, sid, stage):
    """The card's keyboard with its pipeline row swapped for the row of ``stage``; None when the
    message has no pipeline row (a reminder, a report card sent before WP3)."""
    rows, found = [], False
    for row in markup.get("inline_keyboard", []):
        if any(_stage_button(b.get("callback_data"), sid) for b in row):
            rows.append(tracker.card_row(stage, sid))
            found = True
        else:
            rows.append(row)
    if not found:
        return None
    markup["inline_keyboard"] = rows
    return markup


def _quiet(at=None):
    start, end = config.QUIET_HOURS
    hour = (at or dt.datetime.now(dt.timezone.utc)).astimezone(tracker.LONDON).hour
    return hour >= start or hour < end


class Handler:
    """Allowlisted update handler; client.call and request_run are injectable.

    With per-person storage every reply, edit and callback answer of an update is queued while the
    update is applied and sent afterwards (the outbox). The update is then recorded as handled with
    whatever is still unsent, so when sending fails and the poller retries the update, the retry only
    sends what is left: a state change is never applied twice and never lost. A request Telegram
    rejects for good (400/403) is logged and skipped rather than retried forever."""

    def __init__(self, chat_id, request_run, client):
        self.chat_id = str(chat_id)        # the owner's chat
        self.request_run = request_run
        self.client = client
        self.user = None                   # who the update being handled belongs to
        self._outbox = None                # replies queued while an update is applied
        self._update = None                # the update id being applied
        self._reply_to = None              # the message a typed answer replied to

    @property
    def chat(self):
        return self.user.chat_id if self.user is not None and self.user.chat_id else self.chat_id

    # ---------- every message and edit goes through _out ----------

    def _out(self, method, body, **flags):
        """Queue (while applying an update) or send one Telegram request. flags: best_effort (never
        worth retrying: callback answers), fallback (a request to send instead when this one is
        rejected or its message is gone), nonce (bind the pending typed answer to this message)."""
        item = {"method": method, "body": body, **flags}
        if self._outbox is not None:
            self._outbox.append(item)
            return None
        return self._deliver(item)

    def _deliver(self, item):
        try:
            result = self.client.call(item["method"], **item["body"])
        except TelegramError as error:
            if item.get("fallback") and (error.unavailable_message or error.permanent):
                return self._deliver(item["fallback"])
            if error.unavailable_message or item.get("best_effort"):
                LOG.warning("Telegram %s was not accepted; skipped", item["method"])
                return None
            if getattr(self, "_flushing", False) and error.permanent:
                LOG.warning("Telegram %s was not accepted; skipped", item["method"])
                return None
            raise
        if item.get("nonce") and isinstance(result, dict) and type(result.get("message_id")) is int:
            tracker.bind(item["nonce"], result["message_id"])
        return result

    def _flush(self, update_id, outbox):
        self._flushing = True
        try:
            while outbox:
                try:
                    self._deliver(outbox[0])
                except TelegramError:
                    if update_id is not None:
                        tracker.record(update_id, outbox)
                    raise
                outbox.pop(0)
                if update_id is not None:
                    tracker.record(update_id, outbox)
        finally:
            self._flushing = False

    def _apply(self, update_id, work):
        if not users.enabled():
            work()
            return
        if update_id is not None:
            left = tracker.handled(update_id)
            if left is not None:            # a retry: the update was applied; send what is left
                self._flush(update_id, left)
                return
            pending = tracker.applied(update_id)
            if pending is not None:         # applied, but its record was lost to a crash
                ask = pending.get("ask") if pending["kind"] != "done" else None
                outbox = ([{"method": "sendMessage", "body": ask, "nonce": pending.get("nonce")}]
                          if isinstance(ask, dict) else [])
                tracker.record(update_id, outbox)
                self._flush(update_id, outbox)
                return
        self._outbox, self._update = [], update_id
        try:
            work()
            outbox = self._outbox
        finally:
            self._outbox, self._update = None, None
        if update_id is not None:
            tracker.record(update_id, outbox)
        self._flush(update_id, outbox)

    def reply(self, text, chat_id=None):
        self.send(text, chat_id=chat_id)

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
        update_id = update.get("update_id") if type(update.get("update_id")) is int else None
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
                    self._apply(update_id, lambda: self.callback(query, message))
                finally:
                    self.user = None
            return
        message = update.get("message")
        if isinstance(message, dict) and isinstance(message.get("text"), str):
            person = self._person(message.get("chat"), message)
            if person is None:
                return
            target = message.get("reply_to_message")
            target = target.get("message_id") if isinstance(target, dict) else None
            with users.use(person):
                self.user, self._reply_to = person, target if type(target) is int else None
                try:
                    self._apply(update_id, lambda: self.command(message["text"]))
                finally:
                    self.user, self._reply_to = None, None

    def _request(self, kind):
        if self.user is None or self.user.owner:
            return self.request_run(kind)
        return self.request_run(kind, self.user.chat_id)

    def settings(self, words):
        person = self.user or users.User(self.chat_id, owner=True)
        if not users.enabled():
            self.reply("Search settings can only be changed on the VPS bot.")
            return
        if len(words) >= 2 and words[1].lower() == "urgent":
            # Stored with the renter profile, never in params: no settings fingerprint change and
            # no re-baselined search.
            raw = " ".join(words[2:]).strip().lower()
            if raw not in ("on", "off", "yes", "no", "default"):
                self.reply("Usage: <code>/set urgent on</code> or <code>/set urgent off</code>")
                return
            renter.set_urgent(raw in ("on", "yes"))
            self.reply("⚡ Top matches now ring even in quiet hours (22:00–07:00)." if renter.urgent_on()
                       else "🔕 Top matches stay silent in quiet hours, like every other alert.")
            return
        if len(words) < 2:
            urgent = "on" if renter.urgent_on() else "off"
            self.reply(f"<b>🔎 Your search</b>\n{users.describe(person)}\n"
                       f"• <b>urgent</b> {urgent} — ⚡ top matches ring in quiet hours "
                       f"(<code>/set urgent on|off</code>; not a search setting){SETTINGS_HELP}")
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
        if users.enabled() and not text.lstrip().startswith("/"):
            self.text_reply(text)      # an answer to a prompt; other chatter is ignored as before
            return
        words = text.split()
        command = words[0].split("@")[0].lower() if words else ""
        if words:
            words[0] = command
        if command in ("/settings", "/search", "/set"):
            self.settings(words if command == "/set" else words[:1])
            return
        if command in ("/pipeline", "/profile", "/onboard", "/cancel"):
            if not users.enabled():
                self.reply("This command is only available on the VPS bot.")
            else:
                {"/pipeline": self.pipeline, "/profile": self.profile, "/onboard": self.onboard,
                 "/cancel": self.cancel}[command](words)
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
            if command == "/start" and self._new_person():
                self.onboard(words)
            else:
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
            # Callback acknowledgements expire and never determine durable choices: never retried.
            self._out("answerCallbackQuery", {"callback_query_id": query_id,
                                              "text": tgtext.clip(text, tgtext.CALLBACK_ANSWER)},
                      best_effort=True)

        if len(parts) == 3 and parts[:2] == ["r", "ack"] and parts[2]:
            answer("Preference already noted!")
            return
        if parts[0] in WP3_PREFIXES:
            self.wp3_callback(parts, message, markup, answer)
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
        self._edit_markup(message, markup)
        answer(label)

    # ---------- WP3: pipeline, profile, onboarding (VPS only) ----------

    def send(self, text, markup=None, chat_id=None, silent=False):
        """One message, split at line breaks into several when it is over Telegram's 4,096 visible
        UTF-16 units; the buttons (or the reply prompt) go on the last piece."""
        pieces = tgtext.split_html(text)
        result = None
        for i, piece in enumerate(pieces):
            body = {"chat_id": chat_id or self.chat, "text": piece, "parse_mode": "HTML",
                    "disable_web_page_preview": True}
            last = i == len(pieces) - 1
            if markup and last:
                body["reply_markup"] = markup
            if silent:
                body["disable_notification"] = True
            result = self._out("sendMessage", body)
        return result

    def _prompt(self, kind, key, text, placeholder=None, markup=None):
        if markup is None:
            text += "\n<i>/cancel to skip</i>"
            markup = {"force_reply": True,
                      "input_field_placeholder": tgtext.clip(placeholder or "", tgtext.PLACEHOLDER)}
        nonce = os.urandom(6).hex()
        body = {"chat_id": self.chat, "text": tgtext.clip_html(text), "parse_mode": "HTML",
                "disable_web_page_preview": True, "reply_markup": markup}
        return {"method": "sendMessage", "body": body, "nonce": nonce, "expect": {"kind": kind, "key": key}}

    def ask(self, kind, key, text, placeholder=None, markup=None):
        """Ask for an answer (a typed reply, or a tap on ``markup``) and remember that the next answer
        belongs to this question: only a reply to this message, or a plain message, answers it.
        This must be the update's last state change (a crash-replay relies on it)."""
        prompt = self._prompt(kind, key, text, placeholder, markup)
        tracker.expect(kind, key, update=self._update, nonce=prompt["nonce"], ask=prompt["body"])
        self._out(**prompt)

    def _done(self):
        tracker.done(update=self._update)

    def _edit_markup(self, message, markup):
        self._out("editMessageReplyMarkup", {"chat_id": message["chat"]["id"],
                                             "message_id": message["message_id"], "reply_markup": markup})

    def _saved(self):
        out = {}
        for row in d1.query("SELECT key, info FROM saved"):
            try:
                info = json.loads(row["info"])
            except (TypeError, ValueError):
                continue
            if isinstance(info, dict):
                out[row["key"]] = info
        return out

    def _new_person(self):
        """/start opens (or resumes) onboarding only for an invited person who has set nothing yet."""
        return (users.enabled() and self.user is not None and not self.user.owner and not self.user.params
                and tracker.value("onboarded") in (None, "running"))

    def pipeline(self, words):
        text, markup = tracker.view(self._saved(), self.chat)
        self.send(text, markup)

    def cancel(self, words):
        pending = tracker.expected()
        self._done()
        self.reply("OK, cancelled." if pending else "Nothing to cancel.")

    def profile(self, words):
        if len(words) < 2:
            self.reply(renter.show_profile())
            return
        name = renter.field_name(words[1])
        if name is None:
            self.reply("Usage: <code>/profile &lt;field&gt; &lt;value&gt;</code> with one of "
                       + ", ".join(f"<code>{n}</code>" for n in renter.FIELDS) + ". /profile shows yours.")
            return
        raw = " ".join(words[2:]).strip()
        if not raw:
            self.ask("profile", name, f"{renter.FIELDS[name][0]}?", renter.FIELDS[name][1])
            return
        self._profile_answer(name, raw)

    def _profile_answer(self, name, raw):
        try:
            stored = renter.set_field(name, raw)
        except ValueError as error:
            self.reply(f"⚠️ {error}")
            return False
        self._done()
        label = renter.FIELDS[name][0]
        if stored is None:
            self.reply(f"🧹 {label} cleared.")
        elif name == "movein":
            self.reply(f"📅 Move-in set to {renter.show_window(*renter.window())}. Cards now show how each "
                       "home's available date fits.")
        else:
            self.reply(f"✅ {label} saved.")
        return True

    def text_reply(self, text):
        pending = tracker.expected()
        if pending is None:
            return
        bound = pending.get("message_id")
        if self._reply_to is not None and bound is not None and self._reply_to != bound:
            # A reply to an older question (or another message): never let it answer the latest one
            self.reply("⚠️ That replies to an earlier message, not my latest question. Reply to the "
                       "latest question (or just type the answer), or /cancel.")
            return
        kind, key = pending.get("kind"), pending.get("key")
        text = " ".join(text.split())[:500]
        if kind == "onboard":
            self._onboard_answer(int(key), text)
            return
        if kind == "profile":
            self._profile_answer(key, text)
            return
        row = tracker.get(key)
        if row is None:
            self._done()
            return
        if kind == "time":
            try:
                when = tracker.parse_when(text)
            except ValueError as error:
                self.reply(f"⚠️ {error}. Try again, or /cancel.")
                return
            booked = tracker.set_viewing(key, when)
            self._done()
            if booked is None:
                self.reply(f"That home is now at {tracker.LABELS[row['stage']]}, so no viewing time was set.")
                return
            self.reply(f"📅 Viewing booked for <b>{tracker.show_time(booked['viewing_at'])}</b> · "
                       f"{tracker._home(booked)}\nI'll remind you the evening before and two hours before, "
                       "then ask how it went.")
        elif kind == "note":
            tracker.set_field(key, "note", text)
            self._done()
            self.reply("📝 Note saved; it shows in /pipeline.")
        elif kind == "lost":
            noted = tracker.set_field(key, "lost_reason", text, stage="lost")
            self._done()
            self.reply("❌ Reason noted." if noted else "That home is no longer marked lost; reason not saved.")
        else:
            self._done()

    def wp3_callback(self, parts, message, markup, answer):
        if not users.enabled():
            answer("This button works on the VPS bot only")
            return
        kind = parts[0]
        if kind == "o":
            self._onboard_button(parts, message, answer)
            return
        page = 0
        if kind == "P" and len(parts) == 3 and parts[1] == "g":
            answer()
            self._refresh_pipeline(message, parts[2])
            return
        if kind in ("e", "q", "m") and len(parts) == 2:
            code, sid = "e", parts[1]
        elif (kind in ("p", "P") and len(parts) in (3, 4)
              and parts[1] in (*tracker.CODES, "t", "n", "i", "r")):
            code, sid = parts[1], parts[2]
            page = parts[3] if len(parts) == 4 else 0
        elif kind == "v" and len(parts) == 3 and parts[1] in ("1", "2", "3", "4", "5"):
            code, sid = parts[1], parts[2]
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
        if kind == "q":
            answer("✍️ Draft below")
            self.send(self._draft(listing))
            return
        if kind == "m":
            self._photos(listing, message["message_id"], answer)
            return
        if kind == "v":
            row = tracker.get(listing["key"])
            if row is None or row["stage"] in tracker.CLOSED:
                answer("Already " + tracker.LABELS[row["stage"]] if row else "Not tracked yet")
                return
            if row["stage"] in ("enquired", "viewing"):
                tracker.move(listing, "viewed")
            tracker.set_field(listing["key"], "rating", int(code))
            self._edit_markup(message, {"inline_keyboard": [[{"text": f"✓ Rated {code}/5",
                                                               "callback_data": f"p:i:{sid}"}]]})
            answer(f"Rated {code}/5")
            self.ask("note", listing["key"], "📝 Anything to remember about the viewing? Reply with a note.",
                     "damp in the bathroom")
            return
        if kind == "e" and tracker.get(listing["key"]) is not None:
            # An older card still shows ✉️ Enquired: never move a home back, just refresh the card
            label = "Already in /pipeline: " + tracker.LABELS[tracker.get(listing["key"])["stage"]]
            follow = None
        else:
            label, follow = self._stage_action(listing, code)
        answer(label)
        if kind == "P":
            self._refresh_pipeline(message, page)
        else:
            stage = tracker.stage_of(listing["key"])
            updated = _stage_markup(markup, sid, stage)
            if updated is not None:
                self._edit_markup(message, updated)
        if follow:
            follow()

    def _stage_action(self, listing, code):
        """(answer, follow-up) for a pipeline button. Every move is checked against the stage the home
        is at now, so a button left on an old message can never take a home backwards or out of
        won/lost; pressing it again changes nothing. A move and its required follow-up are committed
        together, before the card is refreshed or any messages are sent."""
        key = listing["key"]
        row = tracker.get(key)
        current = row["stage"] if row else ("saved" if key in self._saved() else "new")
        if code == "i":
            return (tracker.LABELS[current] if row else "Not tracked yet"), None
        if code == "r":
            if current not in tracker.CLOSED:
                return "Not closed", None
            back = tracker.reopen(key)
            return "↩️ Reopened" + (f": {tracker.LABELS[back['stage']]}" if back else ""), None
        if code == "n":
            if row is None:
                return "Tap ✉️ Enquired first", None
            return "Reply with a note", lambda: self.ask(
                "note", key, "📝 Reply with a note for this home.", "agent says 3 other viewings booked")
        if code == "t" and current == "viewing":
            return "Reply with the viewing time", lambda: self.ask(
                "time", key, "🕒 When is the viewing? e.g. <code>Sat 11:00</code>, <code>tomorrow 18:30</code> or "
                "<code>14/11 9:15am</code> (UK time).", "Sat 11:00")
        stage = "viewing" if code == "t" else tracker.CODES[code]
        if stage == current:
            return f"Already {tracker.LABELS[stage]}", None
        followup = None
        if stage == "viewing":
            followup = self._prompt(
                "time", key, "🕒 Viewing booked. When is it? e.g. <code>Sat 11:00</code> or <code>14/11 18:30</code> "
                "(UK time).", "Sat 11:00")
        elif stage == "viewed":
            followup = {"method": "sendMessage", "body": {
                "chat_id": self.chat,
                "text": f"👀 <b>How was the viewing?</b>\n{tracker._home(row or listing)}\nRate it 1-5.",
                "parse_mode": "HTML", "disable_web_page_preview": True,
                "reply_markup": tracker.rating_keyboard(listing["sid"])}}
        elif stage == "lost":
            followup = self._prompt(
                "lost", key, "❌ Marked as lost. Why? (optional, e.g. <code>let to someone else</code>)", "let agreed")
        if tracker.move(listing, stage, current=current, update=self._update, followup=followup) is None:
            return f"Already {tracker.LABELS.get(current, current)}", None
        return tracker.LABELS[stage], (lambda: self._out(**followup)) if followup else None

    def _refresh_pipeline(self, message, page=0):
        text, markup = tracker.view(self._saved(), self.chat, page=page)
        body = {"chat_id": message["chat"]["id"], "message_id": message["message_id"], "text": text,
                "parse_mode": "HTML", "disable_web_page_preview": True,
                "reply_markup": markup or {"inline_keyboard": []}}
        fallback = {"method": "sendMessage", "body": {k: v for k, v in body.items() if k != "message_id"}}
        self._out("editMessageText", body, fallback=fallback)

    def _advert(self, key):
        if not archive.enabled():
            return {}
        try:
            ad = archive.advert(key)
            data = json.loads(ad["data"]) if ad else {}
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def _draft(self, listing):
        data = self._advert(listing["key"])
        text, _ = renter.draft({**listing, "available": data.get("available")})
        return text

    def _photos(self, listing, message_id, answer):
        """Send the listing photos the card's header did not show (2-10 as an album, 1 as a photo).
        Which ones it showed was recorded when the card was sent (collage.build reports them); a
        card with no record gets every photo, labelled as such, rather than a guess."""
        from .collage import _is_floorplan
        data = self._advert(listing["key"])
        plan = data.get("floorplan")
        urls = [u for u in dict.fromkeys([data.get("image"), *(data.get("images") or [])])
                if isinstance(u, str) and u.startswith("https://") and u != plan and not _is_floorplan(u)]
        shown = tracker.shown_urls(listing["key"], message_id)
        if shown is None:
            rest, label = urls[:10], f"📷 All {min(len(urls), 10)} photos"
        else:
            rest = [u for u in urls if u not in set(shown)][:10]
            label = f"📷 {len(rest)} more photo{'s' * (len(rest) != 1)}"
        if not rest:
            answer("No more photos stored; use Open listing")
            return
        answer(label)
        caption = tgtext.clip_html("📷 " + html.escape(str(listing.get("address") or "More photos")[:80]),
                                   tgtext.CAPTION)
        fallback = {"method": "sendMessage", "body": {
            "chat_id": self.chat, "text": "📷 Photos unavailable; use Open listing", "parse_mode": "HTML"}}
        if len(rest) == 1:
            self._out("sendPhoto", {"chat_id": self.chat, "photo": rest[0], "caption": caption,
                                    "parse_mode": "HTML"}, fallback=fallback)
        else:
            self._out("sendMediaGroup", {"chat_id": self.chat, "media": [
                {"type": "photo", "media": url, **({"caption": caption, "parse_mode": "HTML"} if i == 0 else {})}
                for i, url in enumerate(rest)]}, fallback=fallback)

    # ---------- onboarding ----------

    def onboard(self, words):
        step = tracker.value("onboard.step")
        resumable = tracker.value("onboarded") in ("paused", "running")
        start = int(step) if step is not None and step.isdigit() and resumable else 0
        start = start if start < len(ONBOARDING) else 0
        tracker.put("onboarded", "running")
        intro = ("👋 <b>Let's set up your search</b> · one question at a time. Tap an answer or type one; "
                 "Skip leaves a setting as it is, Stop pauses (/onboard resumes).")
        if start:
            intro = f"👋 Picking up where you left off (question {start + 1} of {len(ONBOARDING)})."
        self.reply(intro)
        self._ask(start)

    def _ask(self, step):
        name, question, choices = ONBOARDING[step]
        tracker.put("onboard.step", step)
        buttons = [{"text": c, "callback_data": f"o:{step}:{i}"} for i, c in enumerate(choices)]
        rows = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
        rows.append([{"text": "⏭ Skip", "callback_data": f"o:{step}:s"},
                     {"text": "⏸ Stop", "callback_data": f"o:{step}:x"}])
        self.ask("onboard", str(step), f"<b>{step + 1}/{len(ONBOARDING)}</b> {question}",
                 markup={"inline_keyboard": rows})

    def _onboard_button(self, parts, message, answer):
        # The button must answer the question being asked now (its step is in the button), so an
        # old tap never lands on a later question; a replayed update is never applied twice (_apply).
        current = tracker.value("onboard.step")
        if (len(parts) != 3 or tracker.value("onboarded") != "running" or current is None
                or parts[1] != current):
            answer("That question has passed; /onboard resumes")
            return
        step, choice = int(parts[1]), parts[2]
        if choice == "x":
            answer("Paused")
            self._edit_markup(message, {"inline_keyboard": []})
            tracker.put("onboarded", "paused")
            self._onboard_close("⏸ Paused. /onboard picks up from this question.")
            return
        if choice == "s":
            answer("Skipped")
            self._edit_markup(message, {"inline_keyboard": []})
            self._onboard_next(step)
            return
        choices = ONBOARDING[step][2]
        if not choice.isdigit() or int(choice) >= len(choices):
            answer()
            return
        answer("✓ " + choices[int(choice)])
        self._edit_markup(message, {"inline_keyboard": []})
        self._onboard_answer(step, choices[int(choice)])

    def _onboard_answer(self, step, raw):
        name = ONBOARDING[step][0]
        try:
            if name in SEARCH_STEPS:
                self._onboard_search(name, raw)
            else:
                stored = renter.set_field(name, raw)
                if name == "movein" and stored:
                    # Say how the answer was read, so a misread window is caught now, not on a card
                    self.reply(f"📅 Move-in: {renter.show_window(*renter.window())}")
        except ValueError as error:
            self.reply(f"⚠️ {error}. Try again, tap Skip, or /cancel.")
            return
        self._onboard_next(step)

    def _onboard_search(self, name, raw):
        person = self.user or users.owner()
        params = dict(person.params)
        if name == "destination":
            label, lat, lng = _locate(raw)
            if any(type(value) not in (int, float) or not math.isfinite(value) for value in (lat, lng)):
                raise ValueError("Location lookup returned invalid coordinates; try again later")
            if not users.in_london(lat, lng):
                raise ValueError(f"{html.escape(label)} is outside London")
            params[name] = {"name": label, "lat": round(lat, 5), "lng": round(lng, 5)}
        else:
            params[name] = users.parse(name, raw)
        if params != person.params:
            users.save_params(person, params)
            # Same as /set: the next scan quietly re-records matches under the new criteria
            d1.query("DELETE FROM meta WHERE name = 'watch_initialized'")
            tracker.put("onboard.changed", "1")

    def _onboard_next(self, step):
        if step + 1 < len(ONBOARDING):
            self._ask(step + 1)
            return
        tracker.put("onboarded", "done")
        tracker.drop("onboard.step")
        self._onboard_close("✅ <b>All set.</b> /settings and /profile show (and change) your answers; "
                            "/pipeline tracks homes once you tap ✉️ Enquired on a card.")

    def _onboard_close(self, text):
        if tracker.value("onboard.changed"):
            tracker.drop("onboard.changed")
            accepted = self._request("rentals")
            text += ("\n🔎 A scan with your new search has been requested." if accepted
                     else "\n🔎 A scan is already queued; your search applies to the next one.")
        self._done()
        self.reply(text)

    # ---------- reminders and change notices (called from the polling loop) ----------

    def tick(self, at=None):
        """Send due viewing reminders and pipeline change notices to everyone. Never raises; a
        failure is retried on the next tick because nothing is marked sent until it was sent."""
        if not users.enabled():
            return
        for person in users.everyone():
            try:
                with users.use(person):
                    self._tick_person(person.chat_id or self.chat_id, at)
            except Exception:
                # Never log details: HTTP errors can disclose the bot token.
                LOG.warning("Pipeline reminders failed for one person; retrying next tick")

    def _tick_person(self, chat_id, at):
        quiet = _quiet(at)
        for key, flag, text, markup in tracker.due_reminders(at):
            # The two-hour reminder rings even at night: the person booked that viewing.
            self.send(text, markup, chat_id, silent=quiet and flag != "soon")
            tracker.reminded(key, flag)
        # Each change is one archive event shared with the watcher's drop cards and the sweep's
        # "no longer found" message: whichever path tells it first acknowledges it for all.
        tracker.notify_changes(lambda text, markup: self.send(text, markup, chat_id, silent=quiet),
                               limit=NOTICES_PER_TICK)


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
        next_tick = 0.0
        while not stop_event.is_set():
            if time.monotonic() >= next_tick:
                # Reminders run beside polling, never inside it: a failure here must not hold back
                # or replay Telegram updates, and the update offset is untouched.
                next_tick = time.monotonic() + TICK_SECONDS
                try:
                    handler.tick()
                except Exception:
                    LOG.warning("Pipeline reminders failed; retrying next tick")
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
                    try:
                        handler.handle(update)
                    except TelegramError as error:
                        if not error.permanent:
                            raise
                        # Telegram will never accept this reply. Its state change (if any) was
                        # recorded before anything was sent, so skip it instead of blocking everyone.
                        LOG.warning("Telegram rejected a reply for good; update skipped")
                    next_offset = update["update_id"] + 1
                    d1.query("INSERT INTO meta (name, value) VALUES (?, ?) "
                             "ON CONFLICT(name) DO UPDATE SET value = excluded.value",
                             OFFSET_KEY, str(next_offset))
                    offset = next_offset
            except Exception:
                # Do not log exceptions/tracebacks: HTTP errors can disclose bot tokens.
                LOG.warning("Telegram polling/handling failed; retrying unacknowledged updates")
                stop_event.wait(RETRY_SECONDS)
