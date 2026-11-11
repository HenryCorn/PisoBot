"""How late are the alerts? (G1, on the optional foundation; switched on by ALERT_TIMING=1.)

Best-effort records of accepted alerts (VPS, STORAGE=local), in the optional features' OWN database (optional.db), never the primary one.
Receipt recovery cannot prove that every accepted alert has a timing row or correlate recovered receipts with those rows.

  listed_at       the portal's own full listing timestamp (Rightmove firstVisibleDate), when it gives one
  first_fetch_at  when this bot first fetched the advert (the archive's first_seen, read from the primary database read-only)
  sent_at         when the alert was acknowledged (taken at the after_ack hook, in memory)
  via             "head" (full cycle) or "sweep" (deep-sweep judging)

The foundation guarantees the order (Telegram -> seen receipt -> acknowledgement -> this feature) and that this code runs on its own worker
thread, after the run lock is released, never on the delivery or watcher thread. A write that meets contention is RETRIED (the rows stay in the foundation's bounded, aged inbox; every loss is counted and shown on /status). Rows carry no person or advert identifier.
`/status` only reads: it never creates the file or the table. Importing this module does no work.
"""
import datetime as dt
import hashlib
import html
import json
import logging
import math
import os
import sqlite3
import uuid

from . import optional
from .optional import store

log = logging.getLogger(__name__)

NAME = "alert_timing"
WINDOW_DAYS = 7
RETENTION_DAYS = 30
MIN_FOR_P90 = 10             # a 90th percentile of fewer alerts than this is the maximum: not shown
BATCH = 65
REPORT_ROWS = store.MAX_ROWS - 1
RECOVERY_PAGE = 100
RECOVERY_PEOPLE = 4
SEEN_BYTES = 4_000_000
NAMES = {"rm": "Rightmove", "otmr": "OnTheMarket"}
DDL = """
CREATE TABLE IF NOT EXISTS alert_timing (
    id INTEGER PRIMARY KEY, source TEXT NOT NULL, reason TEXT NOT NULL, via TEXT NOT NULL, listed_at TEXT,
    first_fetch_at TEXT, sent_at TEXT NOT NULL, exclusive INTEGER);
CREATE INDEX IF NOT EXISTS alert_timing_sent ON alert_timing (sent_at);
CREATE TABLE IF NOT EXISTS alert_timing_keys (event_key TEXT PRIMARY KEY, sent_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS alert_timing_keys_sent ON alert_timing_keys (sent_at);
CREATE TABLE IF NOT EXISTS alert_timing_unmeasured (
    event_key TEXT PRIMARY KEY, source TEXT NOT NULL, observed_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS alert_timing_unmeasured_observed ON alert_timing_unmeasured (observed_at);
"""
stats = {"retried": 0, "written": 0, "recovery_error": "", "recovery_more": False}
_INSTANCE = uuid.uuid4().hex
_SCAN = {"state": None, "after": 0, "had_error": False}


def _now():
    return dt.datetime.now(dt.timezone.utc)


def enabled():
    return os.environ.get("ALERT_TIMING") == "1"


def receipt_key(token):
    return "receipt:" + hashlib.sha256(token.encode("utf-8")).hexdigest()


def row_for(item):
    """The row an after_ack Item stands for, or None when it was not a card Telegram accepted."""
    if item.state != "sent":
        return None
    exclusive = item.get("exclusive")
    event_key = f"instance:{_INSTANCE}:{item.seq}"
    return {"key": item.get("key"), "source": item.get("source"), "reason": item.reason or "new", "via": item.via or "head",
            "listed_at": item.get("first_listed_at"), "exclusive": None if exclusive is None else int(bool(exclusive)),
            "sent_at": dt.datetime.fromtimestamp(item.at, dt.timezone.utc).isoformat(timespec="seconds"), "seq": item.seq,
            "event_key": event_key}


def first_seen(keys):
    out, failed = {}, set()
    last_error = None
    for key in set(keys):
        found, error = store.read_primary_checked("SELECT first_seen FROM adverts WHERE key = ?", (key,))
        if error is not None:
            failed.add(key)
            last_error = error
        elif found:
            out[key] = found[0]["first_seen"]
    if failed:
        log.warning("timing first-sight lookup failed for %s advert(s) (%s); retrying", len(failed), type(last_error).__name__)
    return out, failed


def write_rows(rows, now=None):
    """Write rows (a small batch per transaction) and apply retention in the same transaction. Returns the rows NOT written."""
    now = now or _now()
    cutoff = (now - dt.timedelta(days=RETENTION_DAYS)).isoformat(timespec="seconds")
    seen, failed = first_seen(r["key"] for r in rows)
    left = [r for r in rows if r["key"] in failed]
    rows = [r for r in rows if r["key"] not in failed]
    for start in range(0, len(rows), BATCH):
        chunk = rows[start:start + BATCH]
        statements = []
        for r in chunk:
            statements.append(("INSERT INTO alert_timing (source, reason, via, listed_at, first_fetch_at, sent_at, exclusive) "
                               "SELECT ?, ?, ?, ?, ?, ?, ? WHERE NOT EXISTS "
                               "(SELECT 1 FROM alert_timing_keys WHERE event_key = ?)",
                               (r["source"], r["reason"], r["via"], r["listed_at"], seen.get(r["key"]), r["sent_at"],
                                r["exclusive"], r["event_key"])))
            statements.append(("INSERT OR IGNORE INTO alert_timing_keys (event_key, sent_at) VALUES (?, ?)",
                               (r["event_key"], r["sent_at"])))
        statements.append(("DELETE FROM alert_timing WHERE sent_at < ?", (cutoff,)))
        statements.append(("DELETE FROM alert_timing_keys WHERE sent_at < ?", (cutoff,)))
        statements.append(("DELETE FROM alert_timing_unmeasured WHERE observed_at < ?", (cutoff,)))
        if store.write(DDL, statements):
            stats["written"] += len(chunk)
        else:
            left.extend(chunk)
    return left


def _seen_for(chat, owner):
    from . import localdb, users
    with users.use(users.User(chat, owner=chat == owner)):
        target = users.seen_path(str(localdb.state_dir() / "seen.json"))
        try:
            with open(target, "rb") as source:
                data = source.read(SEEN_BYTES + 1)
        except FileNotFoundError:
            return {}
    if len(data) > SEEN_BYTES:
        raise ValueError("seen receipt file exceeds recovery limit")
    seen = json.loads(data)
    if type(seen) is not dict:
        raise ValueError("invalid seen receipt file")
    return seen


def recover_receipts(now=None):
    """Record settled receipt tokens with no verifiable timing link, without inventing their send times."""
    from . import localdb, users
    state = str(localdb.state_dir())
    if _SCAN["state"] != state:
        _SCAN.update(state=state, after=0, had_error=False)
    if not _SCAN["after"]:
        _SCAN["had_error"] = False
    owner = users.owner_chat() or "owner"
    rows, error = store.read_primary_checked(
        "SELECT w.rowid AS rid, w.chat_id, w.key, w.intent_id, a.source FROM advert_work w "
        "JOIN adverts a ON a.key = w.key WHERE w.rowid > ? AND w.state = 'done' AND w.notify = 0 "
        "AND (w.chat_id = ? OR EXISTS (SELECT 1 FROM users u WHERE u.chat_id = w.chat_id AND u.active = 1)) "
        "ORDER BY w.rowid LIMIT ?", (_SCAN["after"], owner, RECOVERY_PAGE + 1))
    if error is not None:
        raise RuntimeError("receipt recovery read failed") from error
    more = len(rows) > RECOVERY_PAGE
    people, candidates, processed = {}, {}, []
    for row in rows[:RECOVERY_PAGE]:
        chat = row["chat_id"]
        if chat not in people:
            if len(people) >= RECOVERY_PEOPLE:
                more = True
                break
            try:
                people[chat] = _seen_for(chat, owner)
            except (OSError, ValueError, RecursionError) as ex:
                people[chat] = None
                _SCAN["had_error"] = True
                stats["recovery_error"] = type(ex).__name__
                log.warning("timing receipt file unavailable (%s)", type(ex).__name__)
        processed.append(row)
        if people[chat] is None:
            continue
        receipt = people[chat].get(row["key"])
        token = receipt.get("delivery_intent") if type(receipt) is dict else None
        if (type(token) is str and 0 < len(token) <= 128
                and (row["intent_id"] is None or token == row["intent_id"])):
            candidates[receipt_key(token)] = row["source"]
    if candidates:
        keys = tuple(candidates)
        marks = ",".join("?" for _ in keys)
        sql = (f"SELECT event_key FROM alert_timing_keys WHERE event_key IN ({marks}) UNION "
               f"SELECT event_key FROM alert_timing_unmeasured WHERE event_key IN ({marks})")
        known, error = store.read_checked(sql, keys + keys)
        if isinstance(error, sqlite3.OperationalError) and str(error).startswith("no such table: alert_timing_"):
            if not store.write(DDL, []):
                raise RuntimeError("receipt recovery schema unavailable")
            known, error = store.read_checked(sql, keys + keys)
        if error is not None:
            raise RuntimeError("receipt recovery index read failed") from error
        known = {row["event_key"] for row in known}
        observed = (now or _now()).isoformat(timespec="seconds")
        statements = [
            ("INSERT OR IGNORE INTO alert_timing_unmeasured (event_key, source, observed_at) "
             "SELECT ?, ?, ? WHERE NOT EXISTS (SELECT 1 FROM alert_timing_keys WHERE event_key = ?)",
             (key, source, observed, key)) for key, source in candidates.items() if key not in known]
        if statements:
            cutoff = ((now or _now()) - dt.timedelta(days=RETENTION_DAYS)).isoformat(timespec="seconds")
            statements.append(("DELETE FROM alert_timing_unmeasured WHERE observed_at < ?", (cutoff,)))
            if not store.write(DDL, statements):
                raise RuntimeError("receipt recovery write failed")
    _SCAN["after"] = processed[-1]["rid"] if more and processed else 0
    stats["recovery_more"] = more
    if not more and not _SCAN["had_error"]:
        stats["recovery_error"] = ""


class Feature:
    """The object the foundation's worker thread talks to (never called from the delivery or watcher thread)."""
    name = "alert_timing"

    def after_cycle(self, items):
        rows = [r for r in (row_for(i) for i in items) if r]
        left = write_rows(rows) if rows else []
        try:
            recover_receipts()
        except Exception as ex:
            stats["recovery_error"] = type(ex).__name__
            log.warning("timing receipt recovery failed (%s)", type(ex).__name__)
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


class Report(dict):
    def __init__(self, *, capped=False, error=False):
        super().__init__()
        self.capped, self.error = capped, error


def report(days=WINDOW_DAYS, now=None):
    """{source: {...}} over the last `days`, new-advert alerts only; delays in minutes. READ-ONLY: {} when optional.db or the table does
    not exist (nothing is created). A delay below zero is left out of the figures."""
    now = now or _now()
    since = (now - dt.timedelta(days=days)).isoformat(timespec="seconds")
    rows, error = store.read_checked(
        "SELECT source, via, listed_at, first_fetch_at, sent_at FROM alert_timing "
        "WHERE reason = 'new' AND sent_at >= ? ORDER BY sent_at DESC, id DESC LIMIT ?", (since, REPORT_ROWS + 1))
    if error is not None:
        if isinstance(error, sqlite3.OperationalError) and str(error) == "no such table: alert_timing":
            return Report()
        log.warning("alert timing report read failed (%s)", type(error).__name__)
        return Report(error=True)
    out = Report(capped=len(rows) > REPORT_ROWS)
    rows = rows[:REPORT_ROWS]
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


def unmeasured_count(now):
    since = (now - dt.timedelta(days=WINDOW_DAYS)).isoformat(timespec="seconds")
    rows, error = store.read_checked("SELECT COUNT(*) AS n FROM alert_timing_unmeasured WHERE observed_at >= ?", (since,))
    if isinstance(error, sqlite3.OperationalError) and str(error) == "no such table: alert_timing_unmeasured":
        return 0, None
    if error is not None:
        log.warning("timing receipt recovery status failed (%s)", type(error).__name__)
        return 0, error
    return (rows[0]["n"] if rows else 0), None


def status_lines(now=None):
    """Lines for /status. Nothing (and nothing created) when the feature is off, or no alert was ever recorded."""
    if not enabled():
        return []
    from . import optional
    if not optional.execution_available():
        return [f"\n<b>Alert delay</b>: unavailable ({optional.ISOLATION_REFUSAL})"]
    now = now or _now()
    data = report(now=now)
    lines = []
    if data.error:
        lines.append("\n<b>Alert delay</b>: unavailable (timing database read failed)")
    if data:
        lines.append(f"\n<b>Alert delay</b> (last {WINDOW_DAYS} days, median / 90th percentile; 90th shown from {MIN_FOR_P90} alerts)")
        if data.capped:
            lines.append(f"• showing only the latest {REPORT_ROWS:,} alerts; results are capped")
        for source, r in data.items():
            name = NAMES.get(source, source)
            portal = (f"{_fmt(r['portal_median'])} / {_fmt(r['portal_p90'])} from the portal's listing time ({r['portal_n']} timed)"
                      if r["portal_n"] else "portal listing time not published")
            lines.append(f"• {name}: {r['alerts']} alerts · {portal} · "
                         f"{_fmt(r['fetch_median'])} / {_fmt(r['fetch_p90'])} from our first sight "
                         f"({r['fetch_n']} timed; {r['alerts'] - r['fetch_n']} unavailable)")
        lines.append("• coverage cannot be verified; some accepted alerts may be missing")
    unmeasured, recovery_read_error = unmeasured_count(now)
    if unmeasured:
        prefix = "• " if lines else "\n<b>Alert delay</b>: "
        lines.append(f"{prefix}{unmeasured} receipt(s) noticed by recovery in the last {WINDOW_DAYS} days without a verifiable "
                     "timing link; these may be older or overlap timed alerts")
    if recovery_read_error or stats["recovery_error"]:
        prefix = "• " if lines else "\n<b>Alert delay</b>: "
        lines.append(prefix + "receipt recovery unavailable; it will retry after a cycle")
    elif stats["recovery_more"]:
        prefix = "• " if lines else "\n<b>Alert delay</b>: "
        lines.append(prefix + "receipt recovery scan continues in later cycles")
    health = optional.health()
    mine = [w for w in health["workers"] if w["flag"] == "ALERT_TIMING"]
    waiting = sum(w["waiting"] for w in mine)
    writing = sum(w.get("inflight", 0) for w in mine)       # taken by the worker, the write has not settled yet
    scanning = any(w.get("cycle_pending") for w in mine)
    failed = any(w.get("failed") for w in mine)
    stuck = any(w.get("stuck") for w in mine)
    pending = health.get("pending", 0)
    lost = sum(health["lost"].values())
    refused = health.get("storage") or ""
    if stats["retried"] or pending or waiting or writing or lost or refused:
        text = (f"{pending} awaiting cycle handoff, {waiting} waiting for a free database, {writing} being written now, "
                f"{stats['retried']} retried since start, {lost} dropped "
                f"({', '.join(f'{k} {v}' for k, v in health['lost'].items() if v) or 'none'})")
        if refused:
            text += f" · storage refused: {html.escape(refused)}"
        lines.append(("• not recorded yet: " if lines else "\n<b>Alert delay</b>: ") + text)
    if scanning:
        lines.append(("• " if lines else "\n<b>Alert delay</b>: ") + "receipt recovery scan pending or in progress")
    if failed:
        lines.append(("• " if lines else "\n<b>Alert delay</b>: ") + "timing worker failed; events cannot be recorded")
    if stuck:
        lines.append(("• " if lines else "\n<b>Alert delay</b>: ") + "timing worker stuck; events may be delayed")
    return lines
