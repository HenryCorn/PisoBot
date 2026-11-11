"""How late are the alerts? (G1)

One row per alert Telegram accepted (VPS, STORAGE=local), in the primary SQLite database:

  listed_at       the portal's own full listing timestamp (Rightmove firstVisibleDate), when it gives one
  first_fetch_at  when this bot first fetched the advert (the archive's first_seen, which the release
                  already records; this module adds no second copy)
  sent_at         when Telegram accepted the card (taken in memory at that instant)
  via             "head" (full cycle) or "sweep" (deep-sweep judging)

DELIVERY COMES FIRST, and so does the run lock. `_run` only CAPTURES a row in memory (capture(), no I/O) at the moment
Telegram accepts a card and parks it in an in-memory buffer (defer()); the watcher writes the buffer (flush()) after the
cycle has RELEASED the run lock, so telemetry can delay neither the next person, nor the sweep, nor a queued run. write()
uses a short busy timeout (0.25 s per step), never raises, and a process killed before it runs loses only telemetry. Rows hold no identifier of a person or an advert, only source/reason/path/timestamps, and are
deleted after RETENTION_DAYS (every write and every watch cycle).

report() gives the median and 90th percentile of both delays per source, status_lines() the /status text. Nothing
here sends anything or changes any decision.
"""
import contextlib
import contextvars
import datetime as dt
import logging
import math
import os
import sqlite3
import threading

from . import localdb

log = logging.getLogger(__name__)

VIA = contextvars.ContextVar("pisobot_alert_via", default=None)     # optional path label for callers that set one
BUFFER_LIMIT = 1000
_buffer, _buffer_lock = [], threading.Lock()
WINDOW_DAYS = 7
RETENTION_DAYS = 30
BUSY_SECONDS = 0.25          # telemetry never waits longer than this for the database
NAMES = {"rm": "Rightmove", "otmr": "OnTheMarket"}

DDL = """
CREATE TABLE IF NOT EXISTS alert_timing (
    id INTEGER PRIMARY KEY, source TEXT NOT NULL, reason TEXT NOT NULL, via TEXT NOT NULL, listed_at TEXT,
    first_fetch_at TEXT, sent_at TEXT NOT NULL, exclusive INTEGER);
CREATE INDEX IF NOT EXISTS alert_timing_sent ON alert_timing (sent_at);
"""


def _now():
    return dt.datetime.now(dt.timezone.utc)


@contextlib.contextmanager
def _db():
    """The primary database with a SHORT busy timeout (telemetry is optional: it must not wait on other writers)."""
    token = localdb.DATABASE.set(None)
    try:
        path = localdb.database_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.closing(sqlite3.connect(path, timeout=BUSY_SECONDS)) as db:
            db.row_factory = sqlite3.Row
            db.execute(f"PRAGMA busy_timeout = {int(BUSY_SECONDS * 1000)}")
            db.executescript(DDL)
            with db:
                yield db
    finally:
        localdb.DATABASE.reset(token)


def capture(listing, reason, via=None, sent_at=None):
    """The in-memory record of an accepted alert. NO I/O: safe to call between Telegram and the receipt."""
    exclusive = getattr(listing, "exclusive", None)
    return {"key": listing.key, "source": listing.source, "reason": reason, "via": via or VIA.get() or "head",
            "listed_at": getattr(listing, "first_listed_at", None),
            "exclusive": None if exclusive is None else int(bool(exclusive)),
            "sent_at": (sent_at or _now()).isoformat(timespec="seconds")}


def defer(rows):
    """Park captured rows in memory (no I/O) until flush(); bounded, never raises."""
    with _buffer_lock:
        _buffer.extend(rows)
        del _buffer[:-BUFFER_LIMIT]


def flush(now=None):
    """Write and clear the parked rows (bounded, best effort). Call it where no run lock is held. Never raises."""
    with _buffer_lock:
        rows, _buffer[:] = list(_buffer), []
    return write(rows, now)


def write(rows, now=None):
    """Write captured rows and apply retention, bounded and best effort. Never raises; returns rows written."""
    if not rows or os.environ.get("STORAGE") != "local":
        return 0
    try:
        now = now or _now()
        cutoff = (now - dt.timedelta(days=RETENTION_DAYS)).isoformat(timespec="seconds")
        with _db() as db:
            have = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            for row in rows:
                first = None
                if "adverts" in have:
                    found = db.execute("SELECT first_seen FROM adverts WHERE key = ?", (row["key"],)).fetchone()
                    first = found["first_seen"] if found else None
                db.execute("INSERT INTO alert_timing (source, reason, via, listed_at, first_fetch_at, sent_at, exclusive)"
                           " VALUES (?, ?, ?, ?, ?, ?, ?)",
                           (row["source"], row["reason"], row["via"], row["listed_at"], first, row["sent_at"],
                            row["exclusive"]))
            db.execute("DELETE FROM alert_timing WHERE sent_at < ?", (cutoff,))
        return len(rows)
    except Exception as ex:
        log.warning("timing: not recorded (%s)", type(ex).__name__)
        return 0


def purge_expired(now=None):
    """Retention independent of writes: delete rows older than RETENTION_DAYS. Touches nothing when the table does
    not exist. Never raises."""
    if os.environ.get("STORAGE") != "local":
        return 0
    try:
        now = now or _now()
        token = localdb.DATABASE.set(None)
        try:
            path = localdb.database_path()
            if not os.path.exists(path):
                return 0
            with contextlib.closing(sqlite3.connect(path, timeout=BUSY_SECONDS)) as db:
                if not db.execute("SELECT 1 FROM sqlite_master WHERE name = 'alert_timing'").fetchone():
                    return 0
                with db:
                    return db.execute("DELETE FROM alert_timing WHERE sent_at < ?",
                                      ((now - dt.timedelta(days=RETENTION_DAYS)).isoformat(timespec="seconds"),)).rowcount
        finally:
            localdb.DATABASE.reset(token)
    except Exception as ex:
        log.warning("timing: retention failed (%s)", type(ex).__name__)
        return 0


def _minutes(later, earlier):
    try:
        return (dt.datetime.fromisoformat(later) - dt.datetime.fromisoformat(earlier)).total_seconds() / 60
    except (TypeError, ValueError):
        return None


def percentile(values, q):
    """Nearest-rank percentile of a list (q in 0-100); None for an empty list."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q / 100 * len(ordered)) - 1)]


def report(days=WINDOW_DAYS, now=None):
    """{source: {'alerts', 'portal_n', 'portal_median', 'portal_p90', 'fetch_n', 'fetch_median', 'fetch_p90'}} over the last `days`, new-advert alerts only; delays in minutes. A delay below zero (a portal
    timestamp ahead of our clock) is left out of the figures. Rows without a portal timestamp count in 'alerts'
    and the first-fetch figures only."""
    now = now or _now()
    since = (now - dt.timedelta(days=days)).isoformat(timespec="seconds")
    out = {}
    try:
        with _db() as db:
            rows = [dict(r) for r in db.execute(
                "SELECT source, via, listed_at, first_fetch_at, sent_at FROM alert_timing "
                "WHERE reason = 'new' AND sent_at >= ?", (since,))]
    except Exception as ex:
        log.warning("timing: report unavailable (%s)", type(ex).__name__)
        return out
    for source in sorted({r["source"] for r in rows}):
        mine = [r for r in rows if r["source"] == source]
        portal = [m for r in mine if (m := _minutes(r["sent_at"], r["listed_at"])) is not None and m >= 0]
        fetch = [m for r in mine if (m := _minutes(r["sent_at"], r["first_fetch_at"])) is not None and m >= 0]
        out[source] = {"alerts": len(mine),
                       "portal_n": len(portal), "portal_median": percentile(portal, 50), "portal_p90": percentile(portal, 90),
                       "fetch_n": len(fetch), "fetch_median": percentile(fetch, 50), "fetch_p90": percentile(fetch, 90)}
    return out


def _fmt(minutes):
    if minutes is None:
        return "n/a"
    return f"{minutes:.0f} min" if minutes < 120 else f"{minutes / 60:.1f} h"


def status_lines(now=None):
    """Lines for /status ('' list when nothing was recorded yet)."""
    data = report(now=now)
    if not data:
        return []
    lines = [f"\n<b>Alert delay</b> (last {WINDOW_DAYS} days, median / 90th percentile)"]
    for source, r in data.items():
        name = NAMES.get(source, source)
        portal = (f"{_fmt(r['portal_median'])} / {_fmt(r['portal_p90'])} from the portal's listing time ({r['portal_n']} timed)"
                  if r["portal_n"] else "portal listing time not published")
        lines.append(f"• {name}: {r['alerts']} alerts · {portal} · "
                     f"{_fmt(r['fetch_median'])} / {_fmt(r['fetch_p90'])} from our first sight")
    return lines
