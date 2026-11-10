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

from . import archive, localdb, users

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
"""
# Stages after "saved", in order; "new" and "saved" are implied by the cards and the saved table.
STAGES = ("enquired", "viewing", "viewed", "offer", "won", "lost")
CODES = {"e": "enquired", "b": "viewing", "v": "viewed", "o": "offer", "w": "won", "l": "lost"}
CODE = {stage: code for code, stage in CODES.items()}
LABELS = {"saved": "❤️ Saved", "enquired": "✉️ Enquired", "viewing": "📅 Viewing booked",
          "viewed": "👀 Viewed", "offer": "🤝 Offer made", "won": "🏆 Won", "lost": "❌ Lost"}
NEXT = {"saved": "enquired", "enquired": "viewing", "viewing": "viewed", "viewed": "offer", "offer": "won"}
ACTIVE = ("enquired", "viewing", "viewed", "offer")
AWAIT_MINUTES = 60          # a prompt for a text reply expires after this
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


def query(sql, *params):
    """One statement on the current person's database (the owner's is the primary one)."""
    path = localdb.database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.closing(sqlite3.connect(path, timeout=localdb.BUSY_TIMEOUT_MS / 1000)) as db:
        db.row_factory = sqlite3.Row
        db.execute(f"PRAGMA busy_timeout = {localdb.BUSY_TIMEOUT_MS}")
        localdb._enable_wal(db)
        db.executescript(DDL)
        with db:
            return [dict(row) for row in db.execute(sql, params)]


# ---------- name/value state ----------

def value(name, default=None):
    rows = query("SELECT value FROM renter WHERE name = ?", name)
    return rows[0]["value"] if rows else default


def put(name, text):
    query("INSERT INTO renter (name, value) VALUES (?, ?) ON CONFLICT(name) DO UPDATE SET value = excluded.value",
          name, str(text))


def drop(name):
    query("DELETE FROM renter WHERE name = ?", name)


def expect(kind, key=None, at=None):
    """Remember that the person's next plain-text message answers a prompt (viewing time, note...)."""
    until = (at or now()) + dt.timedelta(minutes=AWAIT_MINUTES)
    put("_await", json.dumps({"kind": kind, "key": key, "until": _iso(until)}))


def expected(at=None):
    """The pending prompt {"kind", "key"}, or None (expired prompts are dropped)."""
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


def done():
    drop("_await")


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


def move(listing, stage, at=None):
    """Put a home at a stage. listing: the ``listings`` row the card's button resolves to
    ({"sid", "key", "url", "address", "cost"}). The archive's current rent and status become the
    baseline for change notices, so moving a home never reports a change by itself."""
    if stage not in STAGES:
        raise ValueError(f"Unknown stage {stage}")
    stamp = _iso(at or now())
    row = get(listing["key"])
    if row is None:
        ad = _archived(listing["key"]) or {}
        query("INSERT INTO pipeline (key, sid, stage, url, address, cost, told_rent, told_status, created_at,"
              " updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
              listing["key"], listing["sid"], stage, listing.get("url"), listing.get("address"),
              listing.get("cost"), ad.get("rent"), ad.get("status"), stamp, stamp)
    else:
        query("UPDATE pipeline SET stage = ?, updated_at = ? WHERE key = ?", stage, stamp, listing["key"])
    return get(listing["key"])


def set_viewing(key, when, at=None):
    """Book the viewing at ``when`` (aware datetime). Reminders whose time has already passed when the
    viewing is booked are marked as sent, so booking tonight for tomorrow morning sends no stale
    "evening before" message."""
    at = at or now()
    flags = [name for name, due in (("eve", _evening_before(when)), ("soon", when - SOON)) if at >= due]
    query("UPDATE pipeline SET stage = 'viewing', viewing_at = ?, reminded = ?, updated_at = ? WHERE key = ?",
          _iso(when), ",".join(flags), _iso(at), key)
    return get(key)


def set_field(key, name, text, at=None):
    if name not in ("rating", "note", "lost_reason"):
        raise ValueError(name)
    query(f"UPDATE pipeline SET {name} = ?, updated_at = ? WHERE key = ?", text, _iso(at or now()), key)


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
        if "eve" not in flags and _evening_before(when) <= at < when - SOON:
            out.append((row["key"], "eve", f"📅 <b>Viewing tomorrow</b> · {time}\n{_home(row)}\n"
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
    """[(key, text, rent, status)] for active pipeline homes whose archived rent or status differs
    from what the person was last told. Call told() after sending so each change is told once."""
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
        lines = []
        if rent is not None and row["told_rent"] is not None and rent != row["told_rent"]:
            arrow = "📉" if rent < row["told_rent"] else "📈"
            lines.append(f"{arrow} Rent £{row['told_rent']:,.0f} → <b>£{rent:,.0f}</b>")
        if status != row["told_status"]:
            if status == "unseen":
                lines.append("No longer found in the search. <i>It may have been let, withdrawn or re-priced "
                             "above the swept range.</i>")
            elif status == "active" and row["told_status"] == "unseen":
                lines.append("Found in the search again.")
        if lines:
            out.append((row["key"], f"📌 <b>{LABELS[row['stage']]} home changed</b>\n{_home(row)}\n"
                                    + "\n".join(lines), rent, status))
        elif rent != row["told_rent"] or status != row["told_status"]:
            told(row["key"], rent, status)
    return out


def told(key, rent, status):
    query("UPDATE pipeline SET told_rent = ?, told_status = ? WHERE key = ?", rent, status, key)


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


def view(saved, chat_id, at=None, limit=20):
    """(text, keyboard) for /pipeline. saved: {key: info} from the saved table; saved homes that have
    a pipeline row are shown at their pipeline stage."""
    entries = rows()
    tracked = {r["key"] for r in entries}
    for key, info in saved.items():
        if key not in tracked:
            entries.append({"key": key, "sid": None, "stage": "saved", "url": info.get("url"),
                            "address": info.get("address"), "cost": info.get("cost")})
    facts = _facts(chat_id, [e["key"] for e in entries])
    order = ("offer", "viewed", "viewing", "enquired", "saved")
    lines, keyboard, n = ["<b>📋 Your pipeline</b>"], [], 0
    shown = [e for stage in order for e in entries if e["stage"] == stage]
    for stage in order:
        group = [e for e in shown[:limit] if e["stage"] == stage]
        if not group:
            continue
        total = sum(e["stage"] == stage for e in shown)
        lines.append(f"\n<b>{LABELS[stage]} ({total})</b>")
        for entry in group:
            n += 1
            fact = facts.get(entry["key"], {})
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
            lines.append(f"{n}. {_home(entry)} · " + " · ".join(parts))
            if entry.get("note"):
                lines.append(f"   📝 <i>{html.escape(entry['note'][:120])}</i>")
            sid = entry["sid"] or _sid(entry["key"])
            keyboard.append(_row(n, entry["stage"], sid, entry))
    closed = [e for e in entries if e["stage"] in ("won", "lost")]
    if closed:
        won = sum(e["stage"] == "won" for e in closed)
        lines.append(f"\n🏆 Won {won} · ❌ Lost {len(closed) - won}")
    if n == 0 and not closed:
        lines.append("Nothing yet. Save a home, or tap ✉️ Enquired on a card once you have contacted the agent.")
    elif len(shown) > limit:
        lines.append(f"\n… and {len(shown) - limit} more")
    return "\n".join(lines), ({"inline_keyboard": keyboard} if keyboard else None)


def _sid(key):
    from .prefs import sid
    return sid(key)


def _row(n, stage, sid, entry):
    """One numbered row of buttons for /pipeline (callback prefix P: re-renders the list)."""
    if stage == "saved":
        return [{"text": f"{n} ✍️ Draft", "callback_data": f"q:{sid}"},
                {"text": f"{n} ✉️ Enquired", "callback_data": f"P:e:{sid}"}]
    buttons = []
    if stage == "viewing":
        buttons.append({"text": f"{n} 🕒 Time", "callback_data": f"P:t:{sid}"})
    nxt = NEXT.get(stage)
    if nxt:
        buttons.append({"text": f"{n} {LABELS[nxt]}", "callback_data": f"P:{CODE[nxt]}:{sid}"})
    buttons.append({"text": f"{n} 📝", "callback_data": f"P:n:{sid}"})
    buttons.append({"text": f"{n} ❌", "callback_data": f"P:l:{sid}"})
    return buttons


def card_row(stage, sid):
    """The stage row a card shows once a home is in the pipeline (callback prefix p: edits the card)."""
    if stage in ("won", "lost"):
        return [{"text": LABELS[stage], "callback_data": f"p:i:{sid}"}]
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
