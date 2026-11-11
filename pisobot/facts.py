"""Optional facts about an alerted home (G2 certificate area/band/value, G6 floorplan area), delivered AFTER the alert.

The alert goes out exactly as the release sends it. Nothing here exists in the path before Telegram accepts a card or before its
receipt and acknowledgement are written. `_run` keeps the accepted message id in a local variable and, after its own receipts,
hands (listing, reply) pairs to defer(), inside a try/except that logs and continues; the rows are written to the feature's OWN
SQLite file (facts.db, see factsdb.py) by the watcher AFTER the cycle has released the run lock and every person's primary
sends are finished (flush()): the main process's only write to facts.db, 0.25 s busy timeout, skipped on contention.

A separate best-effort step then works through the queue as a SUPERVISED CHILD PROCESS (`python -m pisobot.facts`):
  * exactly one step at a time: the child takes an exclusive, non-blocking file lock (facts.lock) and exits if it is held;
  * a hard stop that nothing it does can stall: a SIGALRM watchdog inside the child, a watcher timer that kills it, and the watcher
    checks for an overdue child at the start and end of every cycle too; a memory limit that, if it cannot be set, stops the step;
  * a row is claimed atomically, ONE row at a time, and its attempt is counted only when its lookup actually starts (a row the
    budget never reached is never charged); every recipient keeps their own row and gets their own reply, built from THEIR card's
    snapshot (expensive lookups are shared inside one step only by identical input, never the text);
  * before replying the person must still be active and must not have hidden the home (read-only, short timeout; unknown means no
    reply); the reply is marked 'sending' before it is posted and recorded as sent, with the row done, in ONE transaction, under an
    idempotency key (person, advert, message id): a retry can never post it twice (a kill between the post and the record loses
    the reply rather than repeating it);
  * facts are delivered as a short threaded reply to the sent card (the release records the card's message id but not its text or
    whether it is a photo caption), never as an edit; if it cannot be sent it is dropped after its attempts.

With the features off, or in a dry run, nothing here runs: no file, no table, no child. Importing this module does no work.
"""
import contextlib
import datetime as dt
import json
import logging
import os
import sys
import threading
import time
from dataclasses import fields

from . import budget, config, epc, factsdb, localdb

log = logging.getLogger(__name__)

STEP_SECONDS = config._env("FACTS_STEP_SECONDS", 45)     # whole-step budget
KILL_GRACE_SECONDS = 15                                   # the watcher kills the child this long after its budget
ROW_SECONDS = 15                                          # one recipient's lookups
EXPIRY = dt.timedelta(days=1)
REPLY_MEMORY = dt.timedelta(days=3)
LEASE = dt.timedelta(minutes=5)
MAX_ATTEMPTS = 3
MEMORY_LIMIT_BYTES = 1_500_000_000

DDL = """
CREATE TABLE IF NOT EXISTS facts_wanted (
    id INTEGER PRIMARY KEY, chat_id TEXT NOT NULL, key TEXT NOT NULL, message_id INTEGER NOT NULL,
    created_at TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'wanted',
    lease_until TEXT, payload TEXT NOT NULL, UNIQUE (chat_id, key, message_id));
CREATE TABLE IF NOT EXISTS facts_replies (
    chat_id TEXT NOT NULL, key TEXT NOT NULL, message_id INTEGER NOT NULL, sent_at TEXT NOT NULL,
    PRIMARY KEY (chat_id, key, message_id));
"""
PAYLOAD_FIELDS = ("source", "id", "url", "address", "bedrooms", "scheme", "tenure", "rent", "studio", "floor_area_m2",
                  "floorplan", "full_postcode", "description", "lat", "lng")
_buffer, _buffer_lock = [], threading.Lock()
BUFFER_LIMIT = 500


def floorplan_enabled():
    try:
        from . import floorplan
    except ImportError:
        return False
    return floorplan.enabled()


def enabled():
    """Any optional fact source on (environment checks only, no I/O)."""
    return epc.enabled() or floorplan_enabled()


def _now():
    return dt.datetime.now(dt.timezone.utc)


def _stamp(when):
    return when.isoformat(timespec="seconds")


def _db(create=True):
    return factsdb.connect(DDL, create=create)


# ---------------------------------------------------------------- before the step: capture after the receipt, queue after the lock

def capture(listing, result, chat_id, now=None):
    """What a later step needs from one accepted card: no I/O. None when there is nothing to reply to or nothing a fact could
    be made from."""
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
            "created_at": _stamp(now or _now()), "payload": json.dumps(payload, sort_keys=True)}


def defer(accepted, chat_id):
    """Called by `_run` AFTER its receipts and acknowledgements with the (listing, Telegram reply) pairs of the cards it sent. All the
    work (field access, serialisation) happens here, inside a try/except that logs and continues: it can never lose a receipt.
    The rows wait in memory for flush()."""
    try:
        rows = [r for r in (capture(l, card, chat_id) for l, card in accepted) if r]
        if rows:
            with _buffer_lock:
                _buffer.extend(rows)
                del _buffer[:-BUFFER_LIMIT]
    except Exception as ex:
        log.warning("facts: not queued (%s)", type(ex).__name__)


def flush():
    """Write the parked rows to facts.db: the main process's ONLY write to it. Call it where no run lock is held and after every
    person's sends are done. Short busy timeout, skipped on contention, never raises; not in dry runs (nothing is ever deferred there)."""
    with _buffer_lock:
        rows, _buffer[:] = list(_buffer), []
    if not rows or os.environ.get("STORAGE") != "local" or not enabled():
        return 0
    try:
        with _db() as db:
            before = db.total_changes
            for r in rows:
                db.execute("INSERT OR IGNORE INTO facts_wanted (chat_id, key, message_id, created_at, payload) VALUES (?, ?, ?, ?, ?)",
                           (r["chat_id"], r["key"], r["message_id"], r["created_at"], r["payload"]))
            return db.total_changes - before
    except Exception as ex:
        log.warning("facts: rows not written (%s)", type(ex).__name__)
        return 0


def pending(now=None):
    """Rows a step could still work on. 0 when there is no facts.db (never creates one) or on contention."""
    if os.environ.get("STORAGE") != "local" or not factsdb.exists():
        return 0
    try:
        now = now or _now()
        with factsdb.connect(None, create=False) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name = 'facts_wanted'").fetchone():
                return 0
            return db.execute("SELECT COUNT(*) FROM facts_wanted WHERE state = 'wanted' AND created_at >= ? AND attempts < ?",
                              (_stamp(now - EXPIRY), MAX_ATTEMPTS)).fetchone()[0]
    except Exception:
        return 0


# ---------------------------------------------------------------- person checks (read-only, short timeout)

def person_active(chat_id):
    """True / False / None (unknown): is this person still admitted? The owner always is. Reads the main database read-only."""
    try:
        if str(chat_id) == os.environ.get("TELEGRAM_CHAT_ID", "").strip():
            return True
        with factsdb.read_only(localdb.state_dir() / "pisobot.db") as db:
            row = db.execute("SELECT active FROM users WHERE chat_id = ?", (str(chat_id),)).fetchone()
        return bool(row and row["active"])
    except Exception:
        return None


def home_hidden(chat_id, key, lat, lng):
    """True / False / None (unknown): has this person hidden this home or its building? Reads their database read-only."""
    try:
        from . import users
        owner = str(chat_id) == os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        directory = users.User(str(chat_id), owner=owner).directory
        path = localdb.state_dir() / "pisobot.db" if owner else directory / "pisobot.db"
        with factsdb.read_only(path) as db:
            if db.execute("SELECT 1 FROM hidden WHERE key = ?", (key,)).fetchone():
                return True
            if lat is not None and lng is not None:
                return any(abs(lat - r["lat"]) < 0.0015 and abs(lng - r["lng"]) < 0.0025
                           for r in db.execute("SELECT lat, lng FROM hidden_dev"))
        return False
    except Exception:
        return None


# ---------------------------------------------------------------- the step

def _listing(payload):
    from .models import Listing
    names = {f.name for f in fields(Listing)}
    return Listing(**{k: v for k, v in json.loads(payload).items() if k in names})


def compute(listing, deadline, floorplan_reader=None, epc_client=None, store=None, now=None, memo=None):
    """Establish the facts for one recipient's card snapshot, in place, within `deadline` (a time.monotonic() value): a floorplan
    area (only when the portal stated none), the certificate, then rent per m2 against recently seen PORTAL-stated adverts. The
    advert's own portal-stated area is remembered first as a comparable. `memo` shares the one expensive lookup (the floorplan
    reading) between recipients of the same advert inside ONE step, keyed by the image url; the text is always built per snapshot."""
    token = budget.DEADLINE.set(deadline)
    try:
        if listing.floor_area_m2:
            epc.note_portal_area(listing, store, now)
        if floorplan_enabled() or floorplan_reader is not None:
            from . import floorplan
            reader = floorplan_reader
            if memo is not None and listing.floorplan and not listing.floor_area_m2 and listing.floorplan in memo:
                listing.floorplan_read = dict(memo[listing.floorplan])
                if listing.floorplan_read.get("status") == "read" and listing.floorplan_read.get("area_sqm"):
                    listing.floor_area_m2, listing.area_source = listing.floorplan_read["area_sqm"], "floorplan"
            else:
                floorplan.annotate(listing, reader)
                if memo is not None and listing.floorplan and listing.floorplan_read:
                    memo[listing.floorplan] = dict(listing.floorplan_read)
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


def _housekeeping(now):
    """Recover rows a dead step left behind and apply retention, all inside facts.db."""
    with _db() as db:
        stamp = _stamp(now)
        db.execute("UPDATE facts_wanted SET state = 'wanted', lease_until = NULL WHERE state = 'claimed' AND lease_until < ?", (stamp,))
        # 'sending' with an expired lease: it may or may not have been posted. Never post it again.
        db.execute("UPDATE facts_wanted SET state = 'done', lease_until = NULL WHERE state = 'sending' AND lease_until < ?", (stamp,))
        db.execute("DELETE FROM facts_wanted WHERE created_at < ? OR state = 'done' OR (state = 'wanted' AND attempts >= ?)",
                   (_stamp(now - EXPIRY), MAX_ATTEMPTS))
        db.execute("DELETE FROM facts_replies WHERE sent_at < ?", (_stamp(now - REPLY_MEMORY),))
    epc.purge_expired(now)
    try:
        from . import floorplan
        floorplan.purge_expired(now)
    except ImportError:
        pass


def _candidate(now, skipped):
    with _db() as db:
        row = db.execute("SELECT * FROM facts_wanted WHERE state = 'wanted' AND attempts < ? AND created_at >= ? "
                         f"AND id NOT IN (-1{''.join(',?' * len(skipped))}) ORDER BY id LIMIT 1",
                         (MAX_ATTEMPTS, _stamp(now - EXPIRY), *skipped)).fetchone()
    return dict(row) if row else None


def _claim(row_id, now):
    """Atomically take one row for a lookup that is about to start: counts the attempt, leases it. False if someone else has it."""
    with _db() as db:
        return db.execute("UPDATE facts_wanted SET state = 'claimed', attempts = attempts + 1, lease_until = ? "
                          "WHERE id = ? AND state = 'wanted' AND attempts < ?",
                          (_stamp(now + LEASE), row_id, MAX_ATTEMPTS)).rowcount == 1


def _set(row_id, state, now=None, only=None):
    with _db() as db:
        sql, args = "UPDATE facts_wanted SET state = ?, lease_until = ? WHERE id = ?", [state, _stamp(now + LEASE) if state in ("claimed", "sending") else None, row_id]
        if only:
            sql += " AND state = ?"
            args.append(only)
        return db.execute(sql, args).rowcount == 1


def _already_replied(row):
    with _db() as db:
        return bool(db.execute("SELECT 1 FROM facts_replies WHERE chat_id = ? AND key = ? AND message_id = ?",
                               (row["chat_id"], row["key"], row["message_id"])).fetchone())


def _record_sent(row, now):
    """The reply is recorded as sent AND the row is done, in ONE transaction."""
    with _db() as db:
        db.execute("INSERT OR IGNORE INTO facts_replies (chat_id, key, message_id, sent_at) VALUES (?, ?, ?, ?)",
                   (row["chat_id"], row["key"], row["message_id"], _stamp(now)))
        db.execute("UPDATE facts_wanted SET state = 'done', lease_until = NULL WHERE id = ?", (row["id"],))


def run_step(now=None, seconds=None, send=None, clock=time.monotonic, floorplan_reader=None, epc_client=None, store=None,
             active=None, hidden=None):
    """One best-effort pass over the queue within `seconds`; never raises. Returns {'rows', 'sent', 'dropped', 'waiting'}."""
    import concurrent.futures as futures
    from . import telegram
    send = send or telegram.send_reply
    active = active or person_active
    hidden = hidden or home_hidden
    now = now or _now()
    deadline = clock() + (STEP_SECONDS if seconds is None else seconds)
    stats = {"rows": 0, "sent": 0, "dropped": 0, "waiting": 0}
    try:
        _housekeeping(now)
    except Exception as ex:
        log.warning("facts: queue unavailable (%s); nothing done", type(ex).__name__)
        return stats
    skipped, memo = [], {}
    while clock() < deadline:
        try:
            row = _candidate(now, skipped)
        except Exception as ex:
            log.warning("facts: queue busy (%s)", type(ex).__name__)
            break
        if row is None:
            break
        listing = _listing(row["payload"])
        # Cheap, read-only checks first: a definite 'no' ends the row without a lookup and without an attempt.
        who, hid = active(row["chat_id"]), hidden(row["chat_id"], row["key"], listing.lat, listing.lng)
        if who is False or hid is True:
            with contextlib.suppress(Exception):
                _set(row["id"], "done", now)
            stats["dropped"] += 1
            continue
        if who is None or hid is None:                        # unknown: do not reply (the row waits, uncharged)
            skipped.append(row["id"])
            stats["waiting"] += 1
            continue
        try:
            if not _claim(row["id"], now):
                skipped.append(row["id"])
                continue
        except Exception:
            break
        stats["rows"] += 1
        limit = min(deadline, clock() + ROW_SECONDS)
        pool = futures.ThreadPoolExecutor(max_workers=1)
        future = pool.submit(compute, listing, limit, floorplan_reader, epc_client, store, now, memo)
        try:
            future.result(timeout=max(0.1, limit - clock()))
        except Exception as ex:                                # slow or failing: back to 'wanted' (the attempt stays counted)
            log.warning("facts: %s not established (%s)", row["key"], type(ex).__name__)
            pool.shutdown(wait=False)
            with contextlib.suppress(Exception):
                _set(row["id"], "wanted", only="claimed")
            skipped.append(row["id"])                          # not tried again within this step
            stats["waiting"] += 1
            continue
        pool.shutdown(wait=False)
        text = compose(listing)
        if not text:
            with contextlib.suppress(Exception):
                _set(row["id"], "done", now)
            stats["dropped"] += 1
            continue
        if clock() >= deadline or active(row["chat_id"]) is not True or hidden(row["chat_id"], row["key"], listing.lat, listing.lng) is not False:
            with contextlib.suppress(Exception):               # re-checked just before posting; anything but a clear yes: no reply
                _set(row["id"], "wanted", only="claimed")
            skipped.append(row["id"])
            stats["waiting"] += 1
            continue
        try:
            if _already_replied(row):
                _set(row["id"], "done", now)
                continue
            if not _set(row["id"], "sending", now, only="claimed"):     # committed BEFORE posting: a kill from here never reposts
                continue
        except Exception:
            break
        try:
            send(row["chat_id"], row["message_id"], text)
        except Exception as ex:
            log.warning("facts: reply for %s failed (%s)", row["key"], type(ex).__name__)
            with contextlib.suppress(Exception):
                _set(row["id"], "wanted", only="sending")      # nothing was posted: it may be tried again (next step)
            skipped.append(row["id"])
            stats["waiting"] += 1
            continue
        try:
            _record_sent(row, now)
        except Exception as ex:                                # posted but not recorded: the 'sending' lease expires to done: never again
            log.warning("facts: reply recorded late (%s)", type(ex).__name__)
        stats["sent"] += 1
    return stats


def main():
    """Entry point of the supervised child: python -m pisobot.facts"""
    import fcntl
    import signal
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if os.environ.get("STORAGE") != "local" or not enabled() or not factsdb.exists():
        return 0
    lock = open(factsdb.path().with_suffix(".lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.info("facts step already running; exiting")
        return 0
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (MEMORY_LIMIT_BYTES, MEMORY_LIMIT_BYTES))
        resource.setrlimit(resource.RLIMIT_CPU, (STEP_SECONDS + 20, STEP_SECONDS + 20))
    except Exception as ex:                                    # no memory isolation: do not run the step at all
        if os.environ.get("FACTS_ALLOW_NO_LIMIT") != "1":      # (development machines only, where RLIMIT_AS is not supported)
            log.error("facts step refused: could not set its resource limits (%s)", type(ex).__name__)
            return 3
        log.warning("facts step running WITHOUT resource limits (FACTS_ALLOW_NO_LIMIT=1)")
    signal.alarm(int(STEP_SECONDS + KILL_GRACE_SECONDS - 3))   # the child's own watchdog: default SIGALRM ends the process
    stats = run_step()
    log.info("facts step: %s", stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
