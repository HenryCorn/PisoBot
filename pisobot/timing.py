"""How late are the alerts? (G1)

One row per alert Telegram accepted (VPS, STORAGE=local), in the primary SQLite database:

  listed_at       the portal's own full listing timestamp (Rightmove firstVisibleDate), when it gives one
  first_fetch_at  when this bot first fetched the advert (the archive's first_seen, which the release
                  already records; this module adds no second copy)
  sent_at         when Telegram accepted the card (the send call returned)
  via             "head" (full cycle), "fast" (fast head poll) or "sweep" (deep-sweep judging)

report() gives the median and 90th percentile of both delays per source, status_lines() the /status
text. Recording is best effort: it never raises and never delays or blocks an alert. Nothing here sends
anything or changes any decision.
"""
import contextlib
import contextvars
import datetime as dt
import logging
import math
import os
import sqlite3

from . import archive, localdb

log = logging.getLogger(__name__)

# Set by the fast head poll for the duration of its run so alerts it sends are labelled "fast".
VIA = contextvars.ContextVar("pisobot_alert_via", default=None)
WINDOW_DAYS = 7
NAMES = {"rm": "Rightmove", "otmr": "OnTheMarket"}

DDL = """
CREATE TABLE IF NOT EXISTS alert_timing (
    id INTEGER PRIMARY KEY, chat_id TEXT NOT NULL, key TEXT NOT NULL, source TEXT NOT NULL,
    reason TEXT NOT NULL, via TEXT NOT NULL, listed_at TEXT, first_fetch_at TEXT, sent_at TEXT NOT NULL,
    exclusive INTEGER);
CREATE INDEX IF NOT EXISTS alert_timing_sent ON alert_timing (sent_at);
"""


def _now():
    return dt.datetime.now(dt.timezone.utc)


@contextlib.contextmanager
def _db():
    token = localdb.DATABASE.set(None)
    try:
        path = localdb.database_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.closing(sqlite3.connect(path, timeout=localdb.BUSY_TIMEOUT_MS / 1000)) as db:
            db.row_factory = sqlite3.Row
            db.execute(f"PRAGMA busy_timeout = {localdb.BUSY_TIMEOUT_MS}")
            db.executescript(DDL)
            with db:
                yield db
    finally:
        localdb.DATABASE.reset(token)


def _first_fetch(key):
    with contextlib.suppress(Exception):
        with archive._db() as db:
            row = db.execute("SELECT first_seen FROM adverts WHERE key = ?", (key,)).fetchone()
            return row["first_seen"] if row else None
    return None


def record_alert(chat_id, listing, reason, via=None, sent_at=None):
    """Call right after Telegram accepted a card. Never raises."""
    if os.environ.get("STORAGE") != "local":
        return
    try:
        sent = (sent_at or _now()).isoformat(timespec="seconds")
        exclusive = getattr(listing, "exclusive", None)
        with _db() as db:
            db.execute("INSERT INTO alert_timing (chat_id, key, source, reason, via, listed_at, first_fetch_at,"
                       " sent_at, exclusive) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (str(chat_id), listing.key, listing.source, reason, via or VIA.get() or "head",
                        getattr(listing, "first_listed_at", None), _first_fetch(listing.key), sent,
                        None if exclusive is None else int(bool(exclusive))))
    except Exception as ex:
        log.warning("timing: not recorded (%s)", type(ex).__name__)


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
    """{source: {'alerts', 'portal_n', 'portal_median', 'portal_p90', 'fetch_n', 'fetch_median', 'fetch_p90',
    'fast'}} over the last `days`, new-advert alerts only; delays in minutes. A delay below zero (a portal
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
        out[source] = {"alerts": len(mine), "fast": sum(r["via"] == "fast" for r in mine),
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
                     f"{_fmt(r['fetch_median'])} / {_fmt(r['fetch_p90'])} from our first sight"
                     + (f" · {r['fast']} by the fast poll" if r["fast"] else ""))
    return lines
