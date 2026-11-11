"""How late are the alerts? (G1, on the optional foundation; switched on by ALERT_TIMING=1.)

One row per alert Telegram accepted (VPS, STORAGE=local), in the optional features' OWN database (optional.db), never the primary one:

  listed_at       the portal's own full listing timestamp (Rightmove firstVisibleDate), when it gives one
  first_fetch_at  when this bot first fetched the advert (the archive's first_seen, read from the primary database read-only)
  sent_at         when the alert was acknowledged (taken at the after_ack hook, in memory)
  via             "head" (full cycle) or "sweep" (deep-sweep judging)

The foundation guarantees the order (Telegram -> seen receipt -> acknowledgement -> this feature) and that this code runs on its own worker
thread, after the run lock is released, never on the delivery or watcher thread. A write that meets contention is RETRIED (the rows stay in the foundation's bounded, aged inbox; every loss is counted and shown on /status). Rows carry no person or advert identifier.
`/status` only reads: it never creates the file or the table. Importing this module does no work.
"""
import datetime as dt
import html
import logging
import math
import os

from . import optional
from .optional import store

log = logging.getLogger(__name__)

NAME = "alert_timing"
WINDOW_DAYS = 7
RETENTION_DAYS = 30
MIN_FOR_P90 = 10             # a 90th percentile of fewer alerts than this is the maximum: not shown
BATCH = 100
NAMES = {"rm": "Rightmove", "otmr": "OnTheMarket"}
DDL = """
CREATE TABLE IF NOT EXISTS alert_timing (
    id INTEGER PRIMARY KEY, source TEXT NOT NULL, reason TEXT NOT NULL, via TEXT NOT NULL, listed_at TEXT,
    first_fetch_at TEXT, sent_at TEXT NOT NULL, exclusive INTEGER);
CREATE INDEX IF NOT EXISTS alert_timing_sent ON alert_timing (sent_at);
"""
stats = {"retried": 0, "written": 0}      # in memory; how many rows had to wait for a later cycle / were written


def _now():
    return dt.datetime.now(dt.timezone.utc)


def enabled():
    return os.environ.get("ALERT_TIMING") == "1"


def row_for(item):
    """The row an after_ack Item stands for, or None when it was not a card Telegram accepted."""
    if item.state != "sent":
        return None
    exclusive = item.get("exclusive")
    return {"key": item.get("key"), "source": item.get("source"), "reason": item.reason or "new", "via": item.via or "head",
            "listed_at": item.get("first_listed_at"), "exclusive": None if exclusive is None else int(bool(exclusive)),
            "sent_at": dt.datetime.fromtimestamp(item.at, dt.timezone.utc).isoformat(timespec="seconds"), "seq": item.seq}


def first_seen(keys):
    out = {}
    for key in set(keys):
        found = store.read_primary("SELECT first_seen FROM adverts WHERE key = ?", (key,))
        if found:
            out[key] = found[0]["first_seen"]
    return out


def write_rows(rows, now=None):
    """Write rows (a small batch per transaction) and apply retention in the same transaction. Returns the rows NOT written."""
    now = now or _now()
    cutoff = (now - dt.timedelta(days=RETENTION_DAYS)).isoformat(timespec="seconds")
    seen = first_seen(r["key"] for r in rows)
    left = []
    for start in range(0, len(rows), BATCH):
        chunk = rows[start:start + BATCH]
        statements = [("INSERT INTO alert_timing (source, reason, via, listed_at, first_fetch_at, sent_at, exclusive)"
                       " VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (r["source"], r["reason"], r["via"], r["listed_at"], seen.get(r["key"]), r["sent_at"], r["exclusive"]))
                      for r in chunk]
        statements.append(("DELETE FROM alert_timing WHERE sent_at < ?", (cutoff,)))
        if store.write(DDL, statements):
            stats["written"] += len(chunk)
        else:
            left.extend(chunk)
    return left


class Feature:
    """The object the foundation's worker thread talks to (never called from the delivery or watcher thread)."""
    name = "alert_timing"

    def after_cycle(self, items):
        rows = [r for r in (row_for(i) for i in items) if r]
        if not rows:
            return None
        left = write_rows(rows)
        if not left:
            return None
        stats["retried"] += len(left)
        return [r["seq"] for r in left]                  # the foundation offers exactly these again (bounded, aged, counted)


FEATURE = Feature()


def _minutes(later, earlier):
    try:
        return (dt.datetime.fromisoformat(later) - dt.datetime.fromisoformat(earlier)).total_seconds() / 60
    except (TypeError, ValueError):
        return None


def median(values):
    """The conventional median: the middle value, or the mean of the two middle values for an even count."""
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def percentile(values, q):
    """Nearest-rank percentile of a list (q in 0-100); None for an empty list."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q / 100 * len(ordered)) - 1)]


def report(days=WINDOW_DAYS, now=None):
    """{source: {...}} over the last `days`, new-advert alerts only; delays in minutes. READ-ONLY: {} when optional.db or the table does
    not exist (nothing is created). A delay below zero is left out of the figures."""
    now = now or _now()
    since = (now - dt.timedelta(days=days)).isoformat(timespec="seconds")
    rows = store.read("SELECT source, via, listed_at, first_fetch_at, sent_at FROM alert_timing WHERE reason = 'new' AND sent_at >= ?", (since,))
    out = {}
    for source in sorted({r["source"] for r in rows}):
        mine = [r for r in rows if r["source"] == source]
        portal = [m for r in mine if (m := _minutes(r["sent_at"], r["listed_at"])) is not None and m >= 0]
        fetch = [m for r in mine if (m := _minutes(r["sent_at"], r["first_fetch_at"])) is not None and m >= 0]
        out[source] = {"alerts": len(mine),
                       "portal_n": len(portal), "portal_median": median(portal),
                       "portal_p90": percentile(portal, 90) if len(portal) >= MIN_FOR_P90 else None,
                       "fetch_n": len(fetch), "fetch_median": median(fetch),
                       "fetch_p90": percentile(fetch, 90) if len(fetch) >= MIN_FOR_P90 else None}
    return out


def _fmt(minutes):
    if minutes is None:
        return "–"
    return f"{minutes:.0f} min" if minutes < 120 else f"{minutes / 60:.1f} h"


def status_lines(now=None):
    """Lines for /status. Nothing (and nothing created) when the feature is off, or no alert was ever recorded."""
    if not enabled():
        return []
    from . import optional
    data = report(now=now)
    lines = []
    if data:
        lines.append(f"\n<b>Alert delay</b> (last {WINDOW_DAYS} days, median / 90th percentile; 90th shown from {MIN_FOR_P90} alerts)")
        for source, r in data.items():
            name = NAMES.get(source, source)
            portal = (f"{_fmt(r['portal_median'])} / {_fmt(r['portal_p90'])} from the portal's listing time ({r['portal_n']} timed)"
                      if r["portal_n"] else "portal listing time not published")
            lines.append(f"• {name}: {r['alerts']} alerts · {portal} · "
                         f"{_fmt(r['fetch_median'])} / {_fmt(r['fetch_p90'])} from our first sight")
    health = optional.health()
    mine = [w for w in health["workers"] if w["flag"] == "ALERT_TIMING"]
    waiting = sum(w["waiting"] for w in mine)
    writing = sum(w.get("inflight", 0) for w in mine)       # taken by the worker, the write has not settled yet
    lost = sum(health["lost"].values())
    refused = health.get("storage") or ""
    if stats["retried"] or waiting or writing or lost or refused:
        text = (f"{waiting} waiting for a free database, {writing} being written now, {stats['retried']} retried since start, {lost} dropped "
                f"({', '.join(f'{k} {v}' for k, v in health['lost'].items() if v) or 'none'})")
        if refused:
            text += f" · storage refused: {html.escape(refused)}"
        lines.append(("• not recorded yet: " if data else "\n<b>Alert delay</b>: ") + text)
    return lines
