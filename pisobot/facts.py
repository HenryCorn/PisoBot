"""Optional facts about an alerted home (G2 certificate area/band/value, G6 floorplan area), delivered AFTER the alert.

The alert goes out exactly as the release sends it. Nothing here exists in the path before Telegram accepts a card or
before its receipt and acknowledgement are written: no lookup, no download, no database write. `_run` only CAPTURES, in
memory and with no I/O, what a later step would need (the card's Telegram message id and a few listing fields), and
writes the rows at the very end of the run, after its own receipts, with a short busy timeout, skipping on contention.

A separate, best-effort step then works through that small durable queue (`facts_wanted`):
  * it runs after a cycle's sends are complete, never inside a sweep slice, as a SUPERVISED CHILD PROCESS
    (`python -m pisobot.facts`) with its own time budget; the watcher kills it at the budget, so a hung read, a slow
    host or a large image can never block the next cycle or take the service's memory (the child also sets an
    address-space limit); it never takes the run lock;
  * rows are tried at most MAX_ATTEMPTS times (counted before the work starts, so a poisonous row cannot hog every
    step) and expire after EXPIRY (one day); unfinished rows simply wait for the next cycle;
  * a fact is delivered as a short threaded reply to the already-sent card ('📐 52 m² (floorplan) · £30/m²').
    The release records the card's Telegram message id (tracker.record_shown, from the sendMessage/sendPhoto reply) but not
    the card's text or whether that message is a photo caption or a text message, so editing the card is not safe (it
    would need the exact original text re-rendered); a reply works for both. If it cannot be sent the fact is dropped;
  * every database write uses a short busy timeout (0.25 s) and is skipped on contention.

With both features off nothing in this module runs: no table is created and no row is written.
"""
import concurrent.futures
import contextlib
import datetime as dt
import json
import logging
import os
import sqlite3
import sys
import time
from dataclasses import fields

from . import budget, config, epc, localdb

log = logging.getLogger(__name__)

STEP_SECONDS = config._env("FACTS_STEP_SECONDS", 45)     # whole-step budget; the watcher kills the child 15 s after this
ROW_SECONDS = 15                                          # one advert's lookups
EXPIRY = dt.timedelta(days=1)
MAX_ATTEMPTS = 3
MAX_ROWS = 40
BUSY_SECONDS = 0.25
MEMORY_LIMIT_BYTES = 1_500_000_000

DDL = """
CREATE TABLE IF NOT EXISTS facts_wanted (
    id INTEGER PRIMARY KEY, chat_id TEXT NOT NULL, key TEXT NOT NULL, message_id INTEGER NOT NULL,
    created_at TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, payload TEXT NOT NULL,
    UNIQUE (chat_id, key, message_id));
"""
PAYLOAD_FIELDS = ("source", "id", "url", "address", "bedrooms", "scheme", "tenure", "rent", "studio", "floor_area_m2",
                  "floorplan", "full_postcode", "description")


def floorplan_enabled():
    try:
        from . import floorplan
    except ImportError:
        return False
    return floorplan.enabled()


def enabled():
    """Any optional fact source on (env checks only, no I/O)."""
    return epc.enabled() or floorplan_enabled()


def _now():
    return dt.datetime.now(dt.timezone.utc)


@contextlib.contextmanager
def _db(create=True):
    token = localdb.DATABASE.set(None)
    try:
        path = localdb.database_path()
        if create:
            path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.closing(sqlite3.connect(path, timeout=BUSY_SECONDS)) as db:
            db.row_factory = sqlite3.Row
            db.execute(f"PRAGMA busy_timeout = {int(BUSY_SECONDS * 1000)}")
            if create:
                db.executescript(DDL)
            with db:
                yield db
    finally:
        localdb.DATABASE.reset(token)


# ---------------------------------------------------------------- before the step: capture (memory only) and enqueue

def capture(listing, result, chat_id, now=None):
    """What a later step needs, taken in memory at the instant Telegram accepted the card: NO I/O. None when there is
    nothing a fact could be made from, or no message id to reply to."""
    try:
        message_id = result["result"]["message_id"]
    except (TypeError, KeyError):
        return None
    if type(message_id) is not int or listing.tenure != "rent":
        return None
    if not (listing.floorplan or listing.floor_area_m2 or getattr(listing, "full_postcode", None) or listing.postcode_full):
        return None
    payload = {name: getattr(listing, name, None) for name in PAYLOAD_FIELDS}
    payload["description"] = (payload["description"] or "")[:400]
    return {"chat_id": str(chat_id), "key": listing.key, "message_id": message_id,
            "created_at": (now or _now()).isoformat(timespec="seconds"), "payload": json.dumps(payload, sort_keys=True)}


def enqueue(rows):
    """Write captured rows after the run's receipts: short busy timeout, skipped on contention, never raises."""
    if not rows or os.environ.get("STORAGE") != "local":
        return 0
    try:
        with _db() as db:
            before = db.total_changes
            for r in rows:
                db.execute("INSERT OR IGNORE INTO facts_wanted (chat_id, key, message_id, created_at, payload) "
                           "VALUES (?, ?, ?, ?, ?)", (r["chat_id"], r["key"], r["message_id"], r["created_at"], r["payload"]))
            return db.total_changes - before
    except Exception as ex:
        log.warning("facts: not queued (%s)", type(ex).__name__)
        return 0


def pending(now=None):
    """Rows the next step could work on. 0 when there is no table (never creates one) or on contention."""
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
                if not db.execute("SELECT 1 FROM sqlite_master WHERE name = 'facts_wanted'").fetchone():
                    return 0
                return db.execute("SELECT COUNT(*) FROM facts_wanted WHERE created_at >= ? AND attempts < ?",
                                  ((now - EXPIRY).isoformat(timespec="seconds"), MAX_ATTEMPTS)).fetchone()[0]
        finally:
            localdb.DATABASE.reset(token)
    except Exception:
        return 0


def purge_expired(now=None):
    """Retention for everything the optional features keep (the queue, certificates, floorplan reads, usage counters),
    independent of use and of whether a cycle succeeded. Touches nothing when the tables do not exist. Never raises."""
    deleted = {}
    if os.environ.get("STORAGE") != "local":
        return deleted
    now = now or _now()
    try:
        token = localdb.DATABASE.set(None)
        try:
            path = localdb.database_path()
            if os.path.exists(path):
                with contextlib.closing(sqlite3.connect(path, timeout=BUSY_SECONDS)) as db:
                    if db.execute("SELECT 1 FROM sqlite_master WHERE name = 'facts_wanted'").fetchone():
                        with db:
                            deleted["facts_wanted"] = db.execute(
                                "DELETE FROM facts_wanted WHERE created_at < ? OR attempts >= ?",
                                ((now - EXPIRY).isoformat(timespec="seconds"), MAX_ATTEMPTS)).rowcount
        finally:
            localdb.DATABASE.reset(token)
    except Exception as ex:
        log.warning("facts: queue retention failed (%s)", type(ex).__name__)
    deleted.update(epc.purge_expired(now))
    try:
        from . import floorplan
        deleted.update(floorplan.purge_expired(now))
    except ImportError:
        pass
    return deleted


# ---------------------------------------------------------------- the step

def _listing(payload):
    from .models import Listing
    names = {f.name for f in fields(Listing)}
    return Listing(**{k: v for k, v in json.loads(payload).items() if k in names})


def compute(listing, deadline, floorplan_reader=None, epc_client=None, store=None, now=None):
    """Establish the facts for one advert, in place, within `deadline` (a time.monotonic() value). Order: a floorplan area (only
    when the portal stated none), then the certificate, then rent per m2 against recently seen PORTAL-stated adverts. The
    advert's own portal-stated area is remembered as a comparable first (live runs only: this step never runs in a dry run)."""
    token = budget.DEADLINE.set(deadline)
    try:
        portal_area = listing.floor_area_m2
        if portal_area:
            epc.note_portal_area(listing, store, now)
        if floorplan_enabled() or floorplan_reader is not None:
            from . import floorplan
            floorplan.annotate(listing, floorplan_reader)
        if epc.enabled() or epc_client is not None:
            epc.annotate(listing, epc_client)
        if listing.floor_area_m2:
            epc.value_for(listing, store, now)
    finally:
        budget.DEADLINE.reset(token)
    return listing


def compose(listing):
    """The reply text (HTML-safe) or '' when there is nothing to say."""
    import html
    line = epc.card_line(listing)
    notes = [f"⚠️ Check advert: {n}" for n in getattr(listing, "check_notes", [])]
    parts = ([html.escape(line, quote=False)] if line else []) + [html.escape(n, quote=False) for n in notes]
    return "\n".join(parts)


def _claim(now):
    """Rows to try now: not expired, attempts left; each one's attempt is counted BEFORE any work."""
    cutoff = (now - EXPIRY).isoformat(timespec="seconds")
    with _db() as db:
        rows = [dict(r) for r in db.execute(
            "SELECT * FROM facts_wanted WHERE created_at >= ? AND attempts < ? ORDER BY id LIMIT ?",
            (cutoff, MAX_ATTEMPTS, MAX_ROWS))]
        db.executemany("UPDATE facts_wanted SET attempts = attempts + 1 WHERE id = ?", [(r["id"],) for r in rows])
    return rows


def _finish(ids):
    with contextlib.suppress(Exception):
        with _db() as db:
            db.executemany("DELETE FROM facts_wanted WHERE id = ?", [(i,) for i in ids])


def run_step(now=None, seconds=None, send=None, clock=time.monotonic, floorplan_reader=None, epc_client=None, store=None):
    """One best-effort pass over the queue within `seconds`. Never raises; returns {'rows', 'sent', 'dropped', 'waiting'}."""
    from . import telegram
    send = send or telegram.send_reply
    now = now or _now()
    deadline = clock() + (STEP_SECONDS if seconds is None else seconds)
    stats = {"rows": 0, "sent": 0, "dropped": 0, "waiting": 0}
    try:
        rows = _claim(now)
    except Exception as ex:
        log.warning("facts: queue busy or unavailable (%s); nothing done", type(ex).__name__)
        return stats
    stats["rows"] = len(rows)
    groups = {}
    for r in rows:
        groups.setdefault(r["key"], []).append(r)             # one set of lookups per advert, however many people were told
    for key, group in groups.items():
        if clock() >= deadline:
            stats["waiting"] += len(group)
            continue
        listing = _listing(group[0]["payload"])
        limit = min(deadline, clock() + ROW_SECONDS)
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = pool.submit(compute, listing, limit, floorplan_reader, epc_client, store, now)
        try:
            future.result(timeout=max(0.1, limit - clock()))
        except Exception as ex:                                # a slow or failing lookup: the rows wait for the next step
            log.warning("facts: %s not established (%s)", key, type(ex).__name__)
            stats["waiting"] += len(group)
            pool.shutdown(wait=False)
            continue
        pool.shutdown(wait=False)
        text = compose(listing)
        if not text:
            _finish([r["id"] for r in group])
            stats["dropped"] += len(group)
            continue
        done = []
        for r in group:                                        # sending happens here, never inside the abandonable thread
            try:
                send(r["chat_id"], r["message_id"], text)
                done.append(r["id"])
                stats["sent"] += 1
            except Exception as ex:
                log.warning("facts: reply for %s failed (%s)", key, type(ex).__name__)
                stats["waiting"] += 1
        _finish(done)
    return stats


def main():
    """Entry point of the supervised child: python -m pisobot.facts"""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if os.environ.get("STORAGE") != "local" or not enabled():
        return 0
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (MEMORY_LIMIT_BYTES, MEMORY_LIMIT_BYTES))
    except Exception:
        pass
    stats = run_step()
    log.info("facts step: %s", stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
