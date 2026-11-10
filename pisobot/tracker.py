"""Work package 3 (VPS, STORAGE=local): each person's pipeline of homes, viewing reminders and the
small amount of chat state the bot needs between messages.

Everything lives in the person's own SQLite database (users.use routes it), created on first use, so
worker/schema.sql (shared with the cloud Worker) is untouched. None of it is part of a person's
search settings (users.params), so nothing here changes the settings fingerprint or re-baselines a
search.

pipeline  one row per home a person has moved past Save: stage, viewing time, rating, note, lost
          reason, which reminders were sent, and the archive facts (rent, status) last told
renter    name/value pairs: profile facts (renter.py), the urgent switch, the pending text reply
          and onboarding progress
handled   Telegram updates already applied, with the replies still to send: a replayed update
          (the poller retries until every reply is out) sends what is left and changes nothing
notices   one acknowledgement per archive change (a rent or status episode) shared by every path
          that can tell the person about it: the watcher's price-drop card, a saved home's
          change card, the sweep's "no longer found" and the pipeline's change notice
shown     which listing photos a sent card's header showed, so 📷 More photos sends the others

Saved homes stay in the existing ``saved`` table; /pipeline shows them as the "saved" stage.
Nothing here sends anything to an agent or portal, and nothing makes a network request.
"""
import contextlib
import datetime as dt
import html
import json
import re
import sqlite3
from zoneinfo import ZoneInfo

from . import archive, localdb, tgtext, users

LONDON = ZoneInfo("Europe/London")
DDL = """
CREATE TABLE IF NOT EXISTS pipeline (
    key TEXT PRIMARY KEY,
    sid TEXT NOT NULL,
    stage TEXT NOT NULL,
    url TEXT,
    address TEXT,
    cost REAL,
    viewing_at TEXT,
    rating INTEGER,
    note TEXT,
    lost_reason TEXT,
    reminded TEXT NOT NULL DEFAULT '',
    told_rent REAL,
    told_status TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS pipeline_sid ON pipeline (sid);
CREATE TABLE IF NOT EXISTS renter (name TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS handled (update_id INTEGER PRIMARY KEY, outbox TEXT NOT NULL, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS notices (
    key TEXT NOT NULL,
    kind TEXT NOT NULL,
    event INTEGER NOT NULL,
    state TEXT NOT NULL,
    until TEXT NOT NULL,
    PRIMARY KEY (key, kind, event)
);
CREATE TABLE IF NOT EXISTS shown (
    key TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    urls TEXT NOT NULL,
    at TEXT NOT NULL,
    PRIMARY KEY (key, message_id)
);
"""
# Stages after "saved", in order; "new" and "saved" are implied by the cards and the saved table.
STAGES = ("enquired", "viewing", "viewed", "offer", "won", "lost")
CODES = {"e": "enquired", "b": "viewing", "v": "viewed", "o": "offer", "w": "won", "l": "lost"}
CODE = {stage: code for code, stage in CODES.items()}
LABELS = {"saved": "❤️ Saved", "enquired": "✉️ Enquired", "viewing": "📅 Viewing booked",
          "viewed": "👀 Viewed", "offer": "🤝 Offer made", "won": "🏆 Won", "lost": "❌ Lost"}
NEXT = {"saved": "enquired", "enquired": "viewing", "viewing": "viewed", "viewed": "offer", "offer": "won"}
ACTIVE = ("enquired", "viewing", "viewed", "offer")
CLOSED = ("won", "lost")
RANK = {"new": 0, "saved": 0, "enquired": 1, "viewing": 2, "viewed": 3, "offer": 4, "won": 5}
AWAIT_MINUTES = 60          # a prompt for a text reply expires after this
HANDLED_DAYS = 3            # applied updates are remembered this long (Telegram keeps them 24 hours)
NOTICE_LEASE = dt.timedelta(minutes=10)   # a claimed notice not acknowledged by then may be sent again
PAGE_ENTRIES = 10           # /pipeline homes per page
PAGE_UNITS = 3500           # ... and at most this much visible text (Telegram allows 4096)
EVENING_HOUR = 18           # "the evening before" reminder is due from 18:00 UK time the day before
SOON = dt.timedelta(hours=2)
AFTER = dt.timedelta(hours=1)    # the "how was it?" prompt, this long after the viewing time
STALE = dt.timedelta(days=2)     # never prompt about a viewing older than this
CHECKLIST = ("Bring ID and proof of income or references. Ask about: total move-in cost and deposit "
             "protection, bills and council tax band, broadband, damp or mould, heating and water "
             "pressure, noise, and the earliest move-in date.")


def enabled():
    return users.enabled()


def now():
    return dt.datetime.now(dt.timezone.utc)


def _iso(when):
    return when.astimezone(dt.timezone.utc).isoformat(timespec="seconds")


def _connect():
    path = localdb.database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=localdb.BUSY_TIMEOUT_MS / 1000, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute(f"PRAGMA busy_timeout = {localdb.BUSY_TIMEOUT_MS}")
    localdb._enable_wal(db)
    db.executescript(DDL)
    return db


def query(sql, *params):
    """One statement on the current person's database (the owner's is the primary one)."""
    with contextlib.closing(_connect()) as db:
        return [dict(row) for row in db.execute(sql, params)]


@contextlib.contextmanager
def transaction():
    """A write transaction (BEGIN IMMEDIATE) on the current person's database: the read-then-write
    steps inside it cannot interleave with the watcher's or the poller's."""
    with contextlib.closing(_connect()) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            yield db
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")


# ---------- name/value state ----------

def value(name, default=None):
    rows = query("SELECT value FROM renter WHERE name = ?", name)
    return rows[0]["value"] if rows else default


def put(name, text):
    query("INSERT INTO renter (name, value) VALUES (?, ?) ON CONFLICT(name) DO UPDATE SET value = excluded.value",
          name, str(text))


def drop(name):
    query("DELETE FROM renter WHERE name = ?", name)


def expect(kind, key=None, at=None, update=None, nonce=None, ask=None):
    """Remember that the person's next plain-text message answers a prompt (viewing time, note...).
    update: the Telegram update that asked, so a replay of it knows its answer was already taken;
    nonce: ties the prompt message, once sent, to this pending answer (see bind); ask: the prompt
    {"text", "placeholder"}, resent if the update is replayed after a crash."""
    until = (at or now()) + dt.timedelta(minutes=AWAIT_MINUTES)
    put("_await", json.dumps({"kind": kind, "key": key, "until": _iso(until), "update": update,
                              "nonce": nonce, "message_id": None, "ask": ask}))


def bind(nonce, message_id):
    """The prompt with this nonce went out as message_id: from now on only a reply to that message,
    or a plain message, answers it (a reply to an older prompt does not)."""
    pending = _pending()
    if pending and nonce and pending.get("nonce") == nonce:
        pending["message_id"] = message_id
        put("_await", json.dumps(pending))


def _pending(at=None):
    raw = value("_await")
    if not raw:
        return None
    try:
        pending = json.loads(raw)
        if dt.datetime.fromisoformat(pending["until"]) >= (at or now()):
            return pending
    except (ValueError, KeyError, TypeError):
        pass
    drop("_await")
    return None


def expected(at=None):
    """The pending prompt {"kind", "key", "update", "message_id", ...}, or None (expired prompts are
    dropped)."""
    pending = _pending(at)
    return pending if pending and pending["kind"] != "done" else None


def applied(update_id):
    """The pending-answer record if ``update_id`` was the last update to change it: that update has
    been applied in full (the pending answer is always its last write), even if its record in
    ``handled`` was lost to a crash."""
    pending = _pending()
    if update_id is not None and pending and pending.get("update") == update_id:
        return pending
    return None


def done(update=None):
    """The pending answer was taken. With an update id, a marker stays so that a replay of the update
    that answered is recognised (it expires like a prompt)."""
    if update is None:
        drop("_await")
    else:
        until = now() + dt.timedelta(minutes=AWAIT_MINUTES)
        put("_await", json.dumps({"kind": "done", "key": None, "until": _iso(until), "update": update,
                                  "nonce": None, "message_id": None}))


# ---------- applied updates (replay safety) ----------

def handled(update_id):
    """The outbox still to send for an update already applied, or None if it was never applied."""
    found = query("SELECT outbox FROM handled WHERE update_id = ?", int(update_id))
    return json.loads(found[0]["outbox"]) if found else None


def record(update_id, outbox, at=None):
    """Remember that the update was applied, with the replies it still has to send."""
    at = at or now()
    query("INSERT INTO handled (update_id, outbox, at) VALUES (?, ?, ?) "
          "ON CONFLICT(update_id) DO UPDATE SET outbox = excluded.outbox", int(update_id), json.dumps(outbox), _iso(at))
    query("DELETE FROM handled WHERE at < ?", _iso(at - dt.timedelta(days=HANDLED_DAYS)))


# ---------- pipeline ----------

def get(key):
    rows = query("SELECT * FROM pipeline WHERE key = ?", key)
    return rows[0] if rows else None


def by_sid(sid):
    rows = query("SELECT * FROM pipeline WHERE sid = ?", sid)
    return rows[0] if rows else None


def rows():
    return query("SELECT * FROM pipeline ORDER BY updated_at DESC, key")


def stage_of(key, saved=False):
    row = get(key)
    return row["stage"] if row else ("saved" if saved else "new")


def _archived(key):
    if not archive.enabled():
        return None
    try:
        return archive.advert(key)
    except sqlite3.Error:
        return None


def allowed(current, stage):
    """Whether a home at ``current`` may be put at ``stage``: forward only, "lost" only from an open
    stage, and never out of won/lost (that takes an explicit reopen). The same stage is allowed and
    changes nothing, so replaying a button is harmless."""
    if stage == current:
        return True
    if current in CLOSED:
        return False
    if stage == "lost":
        return current in ("new", "saved") + ACTIVE
    return RANK.get(stage, -1) > RANK.get(current, -1)


def move(listing, stage, at=None, current=None):
    """Put a home at a stage. listing: the ``listings`` row the card's button resolves to
    ({"sid", "key", "url", "address", "cost"}). The archive's current rent and status become the
    baseline for change notices, so moving a home never reports a change by itself.

    Returns the row, or None when the move is not allowed from the stage the home is at now (a stale
    button on an old message). The check and the write are one transaction, so two presses cannot
    race each other past the rules. current: "saved" or "new" for a home not in the pipeline yet."""
    if stage not in STAGES:
        raise ValueError(f"Unknown stage {stage}")
    stamp = _iso(at or now())
    key = listing["key"]
    # Read before taking the write lock: the archive opens its own connection to the same file
    ad = _archived(key) or {}
    with transaction() as db:
        found = db.execute("SELECT stage FROM pipeline WHERE key = ?", (key,)).fetchone()
        now_at = found["stage"] if found else (current if current in ("new", "saved") else "new")
        if not allowed(now_at, stage):
            return None
        if found is None:
            db.execute("INSERT INTO pipeline (key, sid, stage, url, address, cost, told_rent, told_status, created_at,"
                       " updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (key, listing["sid"], stage, listing.get("url"), listing.get("address"),
                        listing.get("cost"), ad.get("rent"), ad.get("status"), stamp, stamp))
        elif now_at != stage:
            db.execute("UPDATE pipeline SET stage = ?, updated_at = ? WHERE key = ? AND stage = ?",
                       (stage, stamp, key, now_at))
        if stage in CLOSED and now_at != stage:
            # Where it was closed from, so a reopen puts it back there (or out of the pipeline)
            db.execute("INSERT INTO renter (name, value) VALUES (?, ?) ON CONFLICT(name) DO UPDATE SET "
                       "value = excluded.value", (f"closed.{key}", now_at))
    return get(key)


def reopen(key, at=None):
    """Take a won or lost home back to the open stage it was closed from: an open stage restores it
    there; a home closed straight from Saved (or a card) leaves the pipeline again."""
    row = get(key)
    if row is None or row["stage"] not in CLOSED:
        return row
    back = value(f"closed.{key}") or "enquired"
    if back in ACTIVE:
        query("UPDATE pipeline SET stage = ?, lost_reason = NULL, updated_at = ? WHERE key = ? AND stage = ?",
              back, _iso(at or now()), key, row["stage"])
    else:
        query("DELETE FROM pipeline WHERE key = ? AND stage = ?", key, row["stage"])
    drop(f"closed.{key}")
    return get(key)


def set_viewing(key, when, at=None):
    """Book the viewing at ``when`` (aware datetime), only while the home is at "viewing" (an answer
    that arrives after the home moved on changes nothing; returns None). Reminders whose time has
    already passed when the viewing is booked are marked as sent, so booking tonight for tomorrow
    morning sends no stale "evening before" message."""
    at = at or now()
    flags = [name for name, due in (("eve", _evening_before(when)), ("soon", when - SOON)) if at >= due]
    with transaction() as db:
        changed = db.execute("UPDATE pipeline SET viewing_at = ?, reminded = ?, updated_at = ? "
                             "WHERE key = ? AND stage = 'viewing'",
                             (_iso(when), ",".join(flags), _iso(at), key)).rowcount
    return get(key) if changed else None


def set_field(key, name, text, at=None, stage=None):
    """Set a rating, note or lost reason; with ``stage``, only while the home is still at that stage.
    Returns whether a row changed."""
    if name not in ("rating", "note", "lost_reason"):
        raise ValueError(name)
    sql = f"UPDATE pipeline SET {name} = ?, updated_at = ? WHERE key = ?" + (" AND stage = ?" if stage else "")
    with transaction() as db:
        return db.execute(sql, (text, _iso(at or now()), key, *((stage,) if stage else ()))).rowcount > 0


def _mark(key, flag):
    row = get(key)
    flags = [f for f in (row["reminded"] or "").split(",") if f] if row else []
    if flag not in flags:
        flags.append(flag)
    query("UPDATE pipeline SET reminded = ? WHERE key = ?", ",".join(flags), key)


# ---------- viewing times ----------

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")


def _clock(text):
    m = re.fullmatch(r"(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm)?", text)
    if not m:
        raise ValueError
    hour, minute, half = int(m[1]), int(m[2] or 0), m[3]
    if half:
        if not 1 <= hour <= 12:
            raise ValueError
        hour = hour % 12 + (12 if half == "pm" else 0)
    elif m[2] is None:
        raise ValueError     # a bare number is a date or a typo, not a time
    if hour > 23 or minute > 59:
        raise ValueError
    return hour, minute


def parse_when(text, at=None):
    """A viewing time typed in Telegram, as an aware UK datetime. Accepts 'today 18:30', 'tomorrow
    9:15am', 'Sat 11:00', '14/11 18:30', '14/11/2026 6pm', '14 Nov 18:30' and '2026-11-14 18:30'.
    Raises ValueError with a short explanation otherwise."""
    at = (at or now()).astimezone(LONDON)
    words = text.strip().lower().replace(",", " ").replace(" at ", " ").split()
    if len(words) < 2:
        raise ValueError("Give a day and a time, e.g. <code>Sat 11:00</code> or <code>14/11 18:30</code>")
    *day_words, clock = words
    if clock in ("am", "pm") and day_words:
        clock = day_words.pop() + clock
    try:
        hour, minute = _clock(clock)
    except ValueError:
        raise ValueError("I couldn't read the time; use 24-hour <code>18:30</code> or <code>6:30pm</code>") from None
    day = " ".join(day_words)
    today = at.date()
    try:
        if day in ("today", "tonight"):
            date = today
        elif day in ("tomorrow", "tmrw", "tmr"):
            date = today + dt.timedelta(days=1)
        elif day[:3] in WEEKDAYS and day.isalpha():
            ahead = (WEEKDAYS.index(day[:3]) - today.weekday()) % 7
            date = today + dt.timedelta(days=ahead)
            if ahead == 0 and (hour, minute) <= (at.hour, at.minute):
                date += dt.timedelta(days=7)
        elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            date = dt.date.fromisoformat(day)
        else:
            m = (re.fullmatch(r"(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?", day)
                 or re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)? ([a-z]{3})[a-z]*(?: (\d{4}))?", day))
            if not m:
                raise ValueError
            month = int(m[2]) if m[2].isdigit() else MONTHS.index(m[2]) + 1
            year = int(m[3]) if m[3] else today.year
            year += 2000 if year < 100 else 0
            date = dt.date(year, month, int(m[1]))
            if not m[3] and date < today:
                date = date.replace(year=today.year + 1)
    except (ValueError, IndexError):
        raise ValueError("I couldn't read the day; try <code>Sat 11:00</code>, <code>tomorrow 9:15</code> "
                         "or <code>14/11 18:30</code>") from None
    when = dt.datetime(date.year, date.month, date.day, hour, minute, tzinfo=LONDON)
    if when < at - dt.timedelta(minutes=5):
        raise ValueError("That time has already passed")
    if when > at + dt.timedelta(days=120):
        raise ValueError("That is more than four months away")
    return when


def show_time(iso):
    return dt.datetime.fromisoformat(iso).astimezone(LONDON).strftime("%a %d %b %H:%M")


def _evening_before(when):
    day = when.astimezone(LONDON).date() - dt.timedelta(days=1)
    return dt.datetime(day.year, day.month, day.day, EVENING_HOUR, tzinfo=LONDON)


def _eve_ends(when):
    """The "evening before" reminder is only sent on the evening before: it stops at midnight starting
    the viewing day (or at the two-hour reminder, for a viewing just after midnight)."""
    day = when.astimezone(LONDON).date()
    return min(dt.datetime(day.year, day.month, day.day, tzinfo=LONDON), when - SOON)


def _day_word(when, at):
    days = (when.astimezone(LONDON).date() - at.astimezone(LONDON).date()).days
    return {0: "today", 1: "tomorrow"}.get(days, when.astimezone(LONDON).strftime("on %A"))


# ---------- reminders and change notices (sent by the Telegram thread) ----------

def _home(row):
    url = html.escape(str(row.get("url") or ""), quote=True)
    address = html.escape(str(row.get("address") or "the home")[:80])
    return f'<a href="{url}">{address}</a>' if url else address


def due_reminders(at=None):
    """[(key, flag, text, keyboard)] for viewings in the current person's pipeline whose reminder is
    due: "eve" (from 18:00 the day before), "soon" (two hours before) and "after" (an hour later: how
    was it?). A reminder whose moment passed while the bot was down is skipped, not sent late."""
    at = at or now()
    out = []
    for row in query("SELECT * FROM pipeline WHERE stage = 'viewing' AND viewing_at IS NOT NULL"):
        when = dt.datetime.fromisoformat(row["viewing_at"])
        flags = set((row["reminded"] or "").split(","))
        time = show_time(row["viewing_at"])
        if "eve" not in flags and _evening_before(when) <= at < _eve_ends(when):
            out.append((row["key"], "eve", f"📅 <b>Viewing {_day_word(when, at)}</b> · {time}\n{_home(row)}\n"
                                          f"<i>{CHECKLIST}</i>", None))
        elif "soon" not in flags and when - SOON <= at < when:
            out.append((row["key"], "soon", f"⏰ <b>Viewing in two hours</b> · {time}\n{_home(row)}", None))
        elif "after" not in flags and when + AFTER <= at < when + STALE:
            out.append((row["key"], "after", f"👀 <b>How was the viewing?</b>\n{_home(row)}\n"
                                            "Rate it 1-5; you can then reply with a note.",
                        rating_keyboard(row["sid"])))
    return out


def reminded(key, flag):
    _mark(key, flag)


def rating_keyboard(sid):
    return {"inline_keyboard": [[{"text": "⭐" * n if n <= 2 else f"{n}⭐", "callback_data": f"v:{n}:{sid}"}
                                 for n in range(1, 6)]]}


def changes():
    """[(key, text, rent, status, parts)] for active pipeline homes whose archived rent or status
    differs from what the person was last told. parts: [(kind, value, line)], one per change, each an
    archive event other paths may already have told (see notify_changes, which sends these once)."""
    if not archive.enabled():
        return []
    out = []
    marks = ",".join("?" for _ in ACTIVE)
    for row in query(f"SELECT * FROM pipeline WHERE stage IN ({marks})", *ACTIVE):
        ad = _archived(row["key"])
        if ad is None:
            continue
        rent, status = ad.get("rent"), ad.get("status")
        if row["told_rent"] is None and row["told_status"] is None:
            told(row["key"], rent, status)        # first sight: the baseline, not a change
            continue
        parts = []
        if rent is not None and row["told_rent"] is not None and rent != row["told_rent"]:
            arrow = "📉" if rent < row["told_rent"] else "📈"
            parts.append(("rent", rent, f"{arrow} Rent £{row['told_rent']:,.0f} → <b>£{rent:,.0f}</b>"))
        if status != row["told_status"]:
            if status == "unseen":
                parts.append(("status", status, "No longer found in the search. <i>It may have been let, "
                                                "withdrawn or re-priced above the swept range.</i>"))
            elif status == "active" and row["told_status"] == "unseen":
                parts.append(("status", status, "Found in the search again."))
        if parts:
            out.append((row["key"], _change_text(row, [line for _, _, line in parts]), rent, status, parts))
        elif rent != row["told_rent"] or status != row["told_status"]:
            told(row["key"], rent, status)
    return out


def _change_text(row, lines):
    return f"📌 <b>{LABELS[row['stage']]} home changed</b>\n{_home(row)}\n" + "\n".join(lines)


def told(key, rent, status):
    query("UPDATE pipeline SET told_rent = ?, told_status = ? WHERE key = ?", rent, status, key)


# ---------- which photos a card showed ----------

def record_shown(key, message_id, urls, at=None):
    """The card sent as ``message_id`` showed these listing photos (URLs) in its header."""
    query("INSERT INTO shown (key, message_id, urls, at) VALUES (?, ?, ?, ?) ON CONFLICT(key, message_id) "
          "DO UPDATE SET urls = excluded.urls, at = excluded.at", key, int(message_id), json.dumps(list(urls)),
          _iso(at or now()))


def shown_urls(key, message_id=None):
    """The photo URLs the card ``message_id`` showed (or, failing that, the latest card for the home);
    None when no card for it was recorded."""
    found = []
    if message_id is not None:
        found = query("SELECT urls FROM shown WHERE key = ? AND message_id = ?", key, int(message_id))
    if not found:
        found = query("SELECT urls FROM shown WHERE key = ? ORDER BY at DESC, message_id DESC LIMIT 1", key)
    return json.loads(found[0]["urls"]) if found else None


# ---------- one acknowledgement per change, shared by every path that tells it ----------

def claim(key, kind, event, at=None):
    """Take the right to tell the person about archive event ``event`` (archive.change_id):
    "claimed" (send it, then ack or release), "told" (someone already did) or "busy" (another path
    is sending it right now; leave it to that path). A claim not acknowledged within NOTICE_LEASE
    (the sender crashed) can be taken again, so a notice is never lost, at worst sent twice."""
    at = at or now()
    with transaction() as db:
        found = db.execute("SELECT state, until FROM notices WHERE key = ? AND kind = ? AND event = ?",
                           (key, kind, event)).fetchone()
        if found and found["state"] == "sent":
            return "told"
        if found and found["until"] > _iso(at):
            return "busy"
        db.execute("INSERT INTO notices (key, kind, event, state, until) VALUES (?, ?, ?, 'pending', ?) "
                   "ON CONFLICT(key, kind, event) DO UPDATE SET state = 'pending', until = excluded.until",
                   (key, kind, event, _iso(at + NOTICE_LEASE)))
    return "claimed"


def ack(key, kind, event, at=None):
    query("INSERT INTO notices (key, kind, event, state, until) VALUES (?, ?, ?, 'sent', ?) "
          "ON CONFLICT(key, kind, event) DO UPDATE SET state = 'sent', until = excluded.until",
          key, kind, event, _iso(at or now()))


def release(key, kind, event):
    query("DELETE FROM notices WHERE key = ? AND kind = ? AND event = ? AND state = 'pending'", key, kind, event)


def once(key, kind, value, send):
    """Call send() unless the person was already told about this change by any path. Returns "sent",
    "told" or "busy" (another path is sending it: do not record it as told). With no archive event to
    name the change by (no archive, or the archive has moved on), it just sends. A send that raises
    releases the claim so the change is retried, except a request Telegram rejected for good
    (``permanent``), which is acknowledged so it is not retried forever."""
    event = archive.change_id(key, kind, value) if archive.enabled() else None
    if event is None:
        send()
        return "sent"
    state = claim(key, kind, event)
    if state != "claimed":
        return state
    try:
        send()
    except Exception as ex:
        (ack if getattr(ex, "permanent", False) else release)(key, kind, event)
        raise
    ack(key, kind, event)
    return "sent"


def notify_changes(send, limit=10):
    """Tell the person about pipeline changes, each archive event once across all paths. send(text,
    keyboard) delivers one message. A change another path is sending right now waits for the next
    tick; one already told only moves this home's baseline. At most ``limit`` messages per call."""
    sent = 0
    for key, _, rent, status, parts in changes():
        if sent >= limit:
            break
        events = [(kind, archive.change_id(key, kind, value), line) for kind, value, line in parts]
        claimed, lines, busy = [], [], False
        for kind, event, line in events:
            state = claim(key, kind, event) if event is not None else "claimed"
            if state == "busy":
                busy = True
            elif state == "claimed":
                claimed.append((kind, event))
                lines.append(line)
        if busy:
            for kind, event in claimed:
                if event is not None:
                    release(key, kind, event)
            continue
        if lines:
            try:
                send(_change_text(get(key), lines), None)
            except Exception as ex:
                for kind, event in claimed:
                    if event is not None:
                        (ack if getattr(ex, "permanent", False) else release)(key, kind, event)
                if not getattr(ex, "permanent", False):
                    raise
            else:
                for kind, event in claimed:
                    if event is not None:
                        ack(key, kind, event)
                sent += 1
        told(key, rent, status)
    return sent


# ---------- /pipeline ----------

def _facts(chat_id, keys):
    """{key: {"rent", "status", "travel_minutes"}} from the archive and this person's verdicts."""
    if not archive.enabled() or not keys:
        return {}
    marks = ",".join("?" for _ in keys)
    with archive._db() as db:
        found = db.execute(f"SELECT a.key, a.rent, a.status, v.travel_minutes FROM adverts a LEFT JOIN verdicts v"
                           f" ON v.key = a.key AND v.chat_id = ? WHERE a.key IN ({marks})", (str(chat_id), *keys))
        return {r["key"]: dict(r) for r in found}


def next_action(row, at=None):
    stage = row["stage"]
    if stage == "saved":
        return "send an enquiry (✍️ draft)"
    if stage == "enquired":
        return "book a viewing"
    if stage == "viewing":
        if not row.get("viewing_at"):
            return "set the viewing time"
        when = dt.datetime.fromisoformat(row["viewing_at"])
        return f"viewing {show_time(row['viewing_at'])}" if when > (at or now()) else "rate the viewing"
    if stage == "viewed":
        return "decide on an offer"
    if stage == "offer":
        return "wait for the agent's answer"
    return ""


def _entry(n, entry, fact, at):
    """The /pipeline lines for one home."""
    parts = []
    if fact.get("rent") is not None:
        parts.append(f"£{fact['rent']:,.0f}")
    elif entry.get("cost"):
        parts.append(f"£{float(entry['cost']):,.0f} all-in")
    if fact.get("travel_minutes") is not None:
        parts.append(f"{fact['travel_minutes']} min")
    if fact.get("status") == "unseen":
        parts.append("⚠️ not found in the last sweeps")
    if entry.get("rating"):
        parts.append("⭐" * int(entry["rating"]))
    parts.append(f"next: {next_action(entry, at)}")
    lines = [f"{n}. {_home(entry)} · " + " · ".join(parts)]
    if entry.get("note"):
        lines.append(f"   📝 <i>{html.escape(entry['note'][:120])}</i>")
    return lines


def view(saved, chat_id, at=None, page=0):
    """(text, keyboard) for one page of /pipeline. saved: {key: info} from the saved table; saved
    homes that have a pipeline row are shown at their pipeline stage. Each page holds at most
    PAGE_ENTRIES homes and PAGE_UNITS of visible text, so it always fits one Telegram message; homes
    are numbered across pages and ◀/▶ buttons move between them."""
    entries = rows()
    tracked = {r["key"] for r in entries}
    for key, info in saved.items():
        if key not in tracked:
            entries.append({"key": key, "sid": None, "stage": "saved", "url": info.get("url"),
                            "address": info.get("address"), "cost": info.get("cost")})
    facts = _facts(chat_id, [e["key"] for e in entries])
    order = ("offer", "viewed", "viewing", "enquired", "saved")
    listed = [e for stage in order for e in entries if e["stage"] == stage]
    counts = {stage: sum(e["stage"] == stage for e in listed) for stage in order}
    pages, current, size = [], [], 0
    for n, entry in enumerate(listed, 1):
        lines = _entry(n, entry, facts.get(entry["key"], {}), at)
        units = tgtext.visible("\n".join(lines)) + 40          # + a group heading
        if current and (len(current) >= PAGE_ENTRIES or size + units > PAGE_UNITS):
            pages.append(current)
            current, size = [], 0
        current.append((n, entry, lines))
        size += units
    if current:
        pages.append(current)
    total = max(1, len(pages))
    try:
        page = min(max(int(page), 0), total - 1)
    except (TypeError, ValueError):
        page = 0
    title = "<b>📋 Your pipeline</b>" + (f" · page {page + 1}/{total}" if total > 1 else "")
    lines, keyboard, heading = [title], [], None
    for n, entry, entry_lines in (pages[page] if pages else []):
        if entry["stage"] != heading:
            heading = entry["stage"]
            lines.append(f"\n<b>{LABELS[heading]} ({counts[heading]})</b>")
        lines.extend(entry_lines)
        keyboard.append(_row(n, entry["stage"], entry["sid"] or _sid(entry["key"]), page))
    closed = [e for e in entries if e["stage"] in CLOSED]
    if closed and page == total - 1:
        won = sum(e["stage"] == "won" for e in closed)
        lines.append(f"\n🏆 Won {won} · ❌ Lost {len(closed) - won}")
    if not listed and not closed:
        lines.append("Nothing yet. Save a home, or tap ✉️ Enquired on a card once you have contacted the agent.")
    nav = []
    if page > 0:
        nav.append({"text": "◀ Previous", "callback_data": f"P:g:{page - 1}"})
    if page < total - 1:
        nav.append({"text": "Next ▶", "callback_data": f"P:g:{page + 1}"})
    if nav:
        keyboard.append(nav)
    return tgtext.clip_html("\n".join(lines)), ({"inline_keyboard": keyboard} if keyboard else None)


def _sid(key):
    from .prefs import sid
    return sid(key)


def _row(n, stage, sid, page=0):
    """One numbered row of buttons for /pipeline (callback prefix P: re-renders the same page)."""
    if stage == "saved":
        return [{"text": f"{n} ✍️ Draft", "callback_data": f"q:{sid}"},
                {"text": f"{n} ✉️ Enquired", "callback_data": f"P:e:{sid}:{page}"}]
    buttons = []
    if stage == "viewing":
        buttons.append({"text": f"{n} 🕒 Time", "callback_data": f"P:t:{sid}:{page}"})
    nxt = NEXT.get(stage)
    if nxt:
        buttons.append({"text": f"{n} {LABELS[nxt]}", "callback_data": f"P:{CODE[nxt]}:{sid}:{page}"})
    buttons.append({"text": f"{n} 📝", "callback_data": f"P:n:{sid}:{page}"})
    buttons.append({"text": f"{n} ❌", "callback_data": f"P:l:{sid}:{page}"})
    return buttons


def card_row(stage, sid):
    """The stage row a card shows once a home is in the pipeline (callback prefix p: edits the card)."""
    if stage in CLOSED:
        return [{"text": LABELS[stage], "callback_data": f"p:i:{sid}"},
                {"text": "↩️ Reopen", "callback_data": f"p:r:{sid}"}]
    buttons = []
    if stage == "viewing":
        buttons.append({"text": "🕒 Set time", "callback_data": f"p:t:{sid}"})
    nxt = NEXT.get(stage)
    if nxt:
        buttons.append({"text": LABELS[nxt], "callback_data": f"p:{CODE[nxt]}:{sid}"})
    if stage in ("viewed", "offer"):
        buttons.append({"text": "📝 Note", "callback_data": f"p:n:{sid}"})
    buttons.append({"text": LABELS["lost"], "callback_data": f"p:l:{sid}"})
    return buttons
