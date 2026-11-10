"""Market archive (VPS, STORAGE=local): what the portals showed and what each person's search made
of it. Everything lives in the primary SQLite database, created on first use, so
worker/schema.sql (shared with the cloud Worker) is untouched.

adverts       one row per source:id with the latest facts, first/last seen, the portal's own
              first-listed date and status active/unseen
observations  a row whenever an advert's rent or status changes (including the first sighting)
verdicts      per person and advert, the latest outcome: "matched" or the first funnel rejection,
              with the numbers behind it
sweeps        one row per deep sweep (sweep.py): scope, requests, adverts seen, adverts the head
              poll had never seen, complete or not, and the resumable crawl state

Nothing here makes a network request.
"""
import contextlib
import datetime as dt
import hashlib
import json
import math
import os
import sqlite3
import uuid

from . import config, localdb
from .sources import restore, snapshot

DDL = """
CREATE TABLE IF NOT EXISTS adverts (
    key TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    url TEXT,
    address TEXT,
    bedrooms INTEGER,
    rent REAL,
    lat REAL,
    lng REAL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    portal_first_listed TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    head_seen INTEGER NOT NULL DEFAULT 0,
    last_sweep_id INTEGER,
    missed INTEGER NOT NULL DEFAULT 0,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS adverts_source_status ON adverts (source, status);
CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY,
    key TEXT NOT NULL,
    at TEXT NOT NULL,
    rent REAL,
    status TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS observations_key ON observations (key, id);
CREATE TABLE IF NOT EXISTS verdicts (
    chat_id TEXT NOT NULL,
    key TEXT NOT NULL,
    outcome TEXT NOT NULL,
    rent REAL,
    all_in REAL,
    travel_minutes INTEGER,
    crime_score INTEGER,
    settings TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (chat_id, key)
);
CREATE INDEX IF NOT EXISTS verdicts_recent ON verdicts (chat_id, updated_at);
CREATE TABLE IF NOT EXISTS sweeps (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    scope TEXT NOT NULL,
    requests INTEGER NOT NULL DEFAULT 0,
    adverts_seen INTEGER NOT NULL DEFAULT 0,
    unseen_by_head INTEGER NOT NULL DEFAULT 0,
    complete INTEGER NOT NULL DEFAULT 0,
    note TEXT,
    state TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS sweeps_source ON sweeps (source, id);
CREATE TABLE IF NOT EXISTS advert_work (
    chat_id TEXT NOT NULL, key TEXT NOT NULL, context TEXT NOT NULL, settings TEXT NOT NULL,
    notify INTEGER NOT NULL DEFAULT 0, silent INTEGER NOT NULL DEFAULT 0,
    failures INTEGER NOT NULL DEFAULT 0, next_retry TEXT, state TEXT NOT NULL,
    updated_at TEXT NOT NULL, intent_id TEXT, PRIMARY KEY(chat_id, key)
);
CREATE INDEX IF NOT EXISTS advert_work_state ON advert_work(chat_id, state);
CREATE TABLE IF NOT EXISTS unseen_events (
    id INTEGER PRIMARY KEY, sweep_id INTEGER NOT NULL, key TEXT NOT NULL,
    UNIQUE(sweep_id, key)
);
CREATE INDEX IF NOT EXISTS unseen_events_key ON unseen_events (key, id);
CREATE TABLE IF NOT EXISTS unseen_deliveries (
    chat_id TEXT NOT NULL, event_id INTEGER NOT NULL,
    PRIMARY KEY(chat_id, event_id)
);
"""
RETRY = "('enrichment', 'unsent', 'deferred', 'enrichment_failed')"
RETRY_BACKOFF_SECONDS = 300  # even the first failure waits: head + sweep share this clock
MAX_RETRY_BACKOFF_SECONDS = 3600  # transient failures never permanently retire an active advert
UNSEEN_AFTER = 2   # consecutive complete sweeps an in-scope advert may be missing from


def enabled():
    return os.environ.get("STORAGE") == "local"


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


@contextlib.contextmanager
def _db():
    """One transaction on the primary database, whichever person is current."""
    token = localdb.DATABASE.set(None)
    try:
        path = localdb.database_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.closing(sqlite3.connect(path, timeout=localdb.BUSY_TIMEOUT_MS / 1000)) as db:
            db.row_factory = sqlite3.Row
            db.execute(f"PRAGMA busy_timeout = {localdb.BUSY_TIMEOUT_MS}")
            localdb._enable_wal(db)
            db.executescript(DDL)
            with db:
                yield db
    finally:
        localdb.DATABASE.reset(token)


def _rows(db, sql, *params):
    return [dict(r) for r in db.execute(sql, params)]


# ---------- adverts ----------

def _facts(data):
    """Decision inputs only: shared snapshots must not invalidate on a person's bill estimate."""
    data = json.loads(data)
    return {key: data.get(key) for key in (
        'rent', 'bedrooms', 'address', 'lat', 'lng', 'description', 'floor_area_m2', 'available',
        'council_tax', 'council_tax_band', 'council_tax_included', 'advert_deposit', 'scheme', 'tenure')}


def upsert(listings, via, sweep_id=None, at=None):
    """Record adverts a fetch returned. via is "head" (the 15-minute poll) or "sweep".
    Returns {"seen": n, "unseen_by_head": n, "new": n} for this call; for a sweep, "seen" counts each
    advert once per sweep and "unseen_by_head" those no head poll had ever shown."""
    at = at or now()
    stats = {"seen": 0, "unseen_by_head": 0, "new": 0}
    with _db() as db:
        for l in listings:
            if l.rent is None:
                continue
            old = (_rows(db, "SELECT * FROM adverts WHERE key = ?", l.key) or [None])[0]
            data = json.dumps(snapshot(l), sort_keys=True)
            if old is None:
                db.execute("INSERT INTO adverts (key, source, url, address, bedrooms, rent, lat, lng, first_seen,"
                           " last_seen, portal_first_listed, status, head_seen, last_sweep_id, missed, data)"
                           " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, 0, ?)",
                           (l.key, l.source, l.url, l.address, l.bedrooms, l.rent, l.lat, l.lng, at, at,
                            l.first_listed, int(via == "head"), sweep_id if via == "sweep" else None, data))
                db.execute("INSERT INTO observations (key, at, rent, status) VALUES (?, ?, ?, 'active')",
                           (l.key, at, l.rent))
                # A newly materialised advert cannot inherit a verdict made without its facts.
                db.execute("DELETE FROM verdicts WHERE key = ?", (l.key,))
                db.execute("UPDATE advert_work SET context = '' WHERE key = ?", (l.key,))
                stats["new"] += 1
                counted, was_head = via == "sweep", False
            else:
                counted = via == "sweep" and old["last_sweep_id"] != sweep_id
                was_head = bool(old["head_seen"])
                db.execute("UPDATE adverts SET url = ?, address = ?, bedrooms = ?, rent = ?, lat = ?, lng = ?,"
                           " last_seen = ?, portal_first_listed = COALESCE(?, portal_first_listed),"
                           " status = 'active', head_seen = ?, last_sweep_id = ?, missed = 0, data = ?"
                           " WHERE key = ?",
                           (l.url, l.address, l.bedrooms, l.rent, l.lat, l.lng, at, l.first_listed,
                            int(was_head or via == "head"),
                            sweep_id if via == "sweep" else old["last_sweep_id"], data, l.key))
                if _facts(old["data"]) != _facts(data) or old["status"] != "active":
                    # Invalidate every person's judgement, never their delivery/seen history.
                    db.execute("DELETE FROM verdicts WHERE key = ?", (l.key,))
                    # Reset retry admission on every actual revision, including reactivation
                    # with identical text/rent. Preserve the sticky notification promise.
                    db.execute("UPDATE advert_work SET context = '' WHERE key = ?", (l.key,))
                if old["rent"] != l.rent or old["status"] != "active":
                    db.execute("INSERT INTO observations (key, at, rent, status) VALUES (?, ?, ?, 'active')",
                               (l.key, at, l.rent))
            if counted:
                stats["seen"] += 1
                stats["unseen_by_head"] += int(not was_head)
        if sweep_id is not None and (stats["seen"] or stats["unseen_by_head"]):
            db.execute("UPDATE sweeps SET adverts_seen = adverts_seen + ?, unseen_by_head = unseen_by_head + ?"
                       " WHERE id = ?", (stats["seen"], stats["unseen_by_head"], sweep_id))
    return stats


def history(key):
    with _db() as db:
        return _rows(db, "SELECT at, rent, status FROM observations WHERE key = ? ORDER BY id", key)


def advert(key):
    with _db() as db:
        rows = _rows(db, "SELECT * FROM adverts WHERE key = ?", key)
    return rows[0] if rows else None


# ---------- verdicts ----------

def _context(settings, data):
    return hashlib.sha256(json.dumps([settings, _facts(data)], sort_keys=True).encode()).hexdigest()


def _job(db, chat_id, key):
    rows = _rows(db, "SELECT * FROM advert_work WHERE chat_id = ? AND key = ?", str(chat_id), key)
    return rows[0] if rows else None


def _context_for(db, key, settings):
    row = db.execute("SELECT data FROM adverts WHERE key = ?", (key,)).fetchone()
    return _context(settings, row["data"] if row else "{}")


def _waiting(job, context, at):
    if not job or job["context"] != context:
        return None
    # Round-2 databases may contain terminal failures. Admit them again on the slow
    # retry clock, preserving provenance, rather than silently abandoning live homes.
    if job["state"] == "failed" and not job["next_retry"]:
        due = dt.datetime.fromisoformat(job["updated_at"]) + dt.timedelta(seconds=MAX_RETRY_BACKOFF_SECONDS)
        if due > dt.datetime.fromisoformat(at):
            return "retry_backoff"
    if job["next_retry"] and dt.datetime.fromisoformat(job["next_retry"]) > dt.datetime.fromisoformat(at):
        return "retry_backoff"
    return None


def begin_lookup(chat_id, key, settings, baseline=False):
    """Persist evaluation provenance BEFORE external I/O, including an interrupted lookup.

    Evaluating is not yet a promise that a home fits; a final rejection may resolve it.
    But a later quiet evaluator cannot reinterpret a live-origin lookup as baseline inventory.
    """
    with _db() as db:
        context = _context_for(db, key, settings)
        old = _job(db, chat_id, key)
        same = old is not None and old["context"] == context
        legacy = db.execute("SELECT outcome FROM verdicts WHERE chat_id=? AND key=?",
                            (str(chat_id), key)).fetchone() if old is None else None
        notify = bool(old and old["notify"]) or bool(legacy and legacy["outcome"] in
                                                    ("unsent", "enrichment", "deferred"))
        live_lookup = old is not None and old["state"] == "evaluating" and not old["silent"]
        silent = ((bool(old["silent"]) if same else False) or bool(baseline)) and not (notify or live_lookup)
        intent_id = (old.get("intent_id") if old and old["notify"] else None)
        if notify and intent_id is None:
            intent_id = uuid.uuid4().hex
        db.execute("INSERT INTO advert_work (chat_id,key,context,settings,notify,silent,failures,next_retry,"
                   "state,updated_at,intent_id) VALUES (?,?,?,?,?,?,?,NULL,'evaluating',?,?) "
                   "ON CONFLICT(chat_id,key) DO UPDATE SET context=excluded.context,settings=excluded.settings,"
                   "notify=excluded.notify,silent=excluded.silent,failures=excluded.failures,"
                   "next_retry=NULL,state='evaluating',updated_at=excluded.updated_at,intent_id=excluded.intent_id",
                   (str(chat_id), key, context, settings, int(notify), int(silent),
                    old["failures"] if same else 0, now(), intent_id))


def retry_block(chat_id, key, settings, at=None):
    """Head and sweep obey the SAME per-advert retry clock. New facts/settings reset it."""
    with _db() as db:
        return _waiting(_job(db, chat_id, key), _context_for(db, key, settings), at or now())


def intent_token(chat_id, key):
    """Receipt identity for the outstanding obligation, not just a price/seen coincidence."""
    with _db() as db:
        job = _job(db, chat_id, key)
    return job.get("intent_id") if job and job["notify"] else None


def silent_keys(chat_id, keys, baseline=False):
    """Sticky live intents win over EVERY quiet path; failed baseline members stay silent later."""
    if not keys:
        return set()
    with _db() as db:
        jobs = {key: _job(db, chat_id, key) for key in keys}
    return {key for key, job in jobs.items()
            if job and not job["notify"] and (baseline or job["silent"])}


def work_counts(chat_id):
    with _db() as db:
        counts = {r["state"]: r["n"] for r in _rows(
            db, "SELECT state, COUNT(*) n FROM advert_work WHERE chat_id = ? GROUP BY state", str(chat_id))}
        counts["intent"] = db.execute("SELECT COUNT(*) FROM advert_work WHERE chat_id = ? AND notify = 1",
                                     (str(chat_id),)).fetchone()[0]
        return counts


def failed_work(chat_id, limit=5):
    with _db() as db:
        return _rows(db, "SELECT w.key, w.failures, a.url, a.address FROM advert_work w "
                     "LEFT JOIN adverts a ON a.key = w.key WHERE w.chat_id = ? AND w.state = 'failed' "
                     "ORDER BY w.updated_at DESC, w.key LIMIT ?", str(chat_id), limit)


def lookup_degraded(chat_id, settings):
    """Current active lookup retries only; stale contexts/removed inventory cannot hold an alarm."""
    with _db() as db:
        rows = _rows(db, "SELECT w.context, a.data FROM advert_work w JOIN adverts a ON a.key=w.key "
                     "WHERE w.chat_id=? AND a.status='active' AND w.state IN ('backoff','failed') "
                     "AND (w.failures > 0 OR w.state='failed')",
                     str(chat_id))
    return sum(row['context'] == _context(settings, row['data']) for row in rows)


def record_verdicts(chat_id, rows, settings, at=None, *, baseline=None, notice_keys=None, acknowledged=False,
                    suppressed_keys=()):
    """Atomically persist verdict AND durable work provenance; a verdict is NOT a delivery receipt.

    notify is sticky across quiet runs, settings and fact revisions until explicit acknowledgement.
    Silent membership survives baseline completion for retries of that same context. Lookup
    failures back off up to an hour, never permanently fail. Explicit suppression retires an
    obsolete notice condition; it is not a fabricated delivery receipt.
    """
    if acknowledged and baseline is True:
        raise ValueError("A silent baseline is not a delivery receipt")
    at = at or now()
    chat_id = str(chat_id)
    with _db() as db:
        for key, outcome, rent, all_in, travel, crime in rows:
            context = _context_for(db, key, settings)
            old = _job(db, chat_id, key)
            same = old is not None and old["context"] == context
            legacy = db.execute("SELECT outcome FROM verdicts WHERE chat_id = ? AND key = ?",
                                (chat_id, key)).fetchone() if old is None else None
            # Unknown old retry provenance is never silently acknowledged during migration.
            inherited = bool(old and old["notify"]) or bool(legacy and legacy["outcome"] in
                                                            ("unsent", "enrichment", "deferred"))
            live_lookup = same and old["state"] == "evaluating" and not old["silent"]
            silent = (bool(old["silent"]) if same and old["state"] == "evaluating"
                      else (bool(old["silent"]) if same else False) or baseline is True)
            candidate = notice_keys is None or key in notice_keys
            retryable = outcome in ("unsent", "enrichment", "deferred")
            notify = inherited or (retryable and candidate and (baseline is not True or live_lookup) and not silent)
            intent_id = old.get("intent_id") if old and old["notify"] else None
            if notify:
                silent = False
                intent_id = intent_id or uuid.uuid4().hex
            failures = old["failures"] if same else 0
            next_retry = None
            if key in suppressed_keys or outcome == "hidden":
                state, notify, intent_id, failures = "done", False, None, 0
            elif outcome == "enrichment":
                failures += 1
                state = "backoff"
                delay = min(MAX_RETRY_BACKOFF_SECONDS, RETRY_BACKOFF_SECONDS * 2 ** min(failures - 1, 4))
                next_retry = (dt.datetime.fromisoformat(at) + dt.timedelta(seconds=delay)).isoformat(
                    timespec="seconds")
            elif outcome == "deferred":
                # Yielding is not a failed lookup, but a short cooldown keeps an
                # individually over-budget advert from burning every following slice.
                state = "backoff"
                next_retry = (dt.datetime.fromisoformat(at) + dt.timedelta(
                    seconds=RETRY_BACKOFF_SECONDS)).isoformat(timespec="seconds")
            elif retryable:
                state = "pending"
                if outcome == "unsent":
                    failures = 0  # lookup recovered; delivery quota is not a lookup failure
            elif outcome == "matched":
                failures = 0  # a successful judgement ends the consecutive lookup failure streak
                if notify and not acknowledged:
                    # Even a buggy caller's baseline settlement cannot clear a live intent.
                    state, outcome = "pending", "unsent"
                else:
                    state, notify = "done", False
            else:
                state = "blocked" if notify else "done"  # pending intent, currently outside criteria
            db.execute("INSERT INTO advert_work (chat_id,key,context,settings,notify,silent,failures,next_retry,"
                       "state,updated_at,intent_id) VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(chat_id,key) DO UPDATE SET "
                       "context=excluded.context,settings=excluded.settings,notify=excluded.notify,"
                       "silent=excluded.silent,failures=excluded.failures,next_retry=excluded.next_retry,"
                       "state=excluded.state,updated_at=excluded.updated_at,intent_id=excluded.intent_id",
                       (chat_id, key, context, settings, int(notify), int(silent), failures, next_retry, state, at, intent_id))
            db.execute(
                "INSERT INTO verdicts (chat_id,key,outcome,rent,all_in,travel_minutes,crime_score,settings,"
                "updated_at) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(chat_id,key) DO UPDATE SET "
                "outcome=excluded.outcome,rent=excluded.rent,all_in=excluded.all_in,"
                "travel_minutes=excluded.travel_minutes,crime_score=excluded.crime_score,"
                "settings=excluded.settings,updated_at=excluded.updated_at",
                (chat_id, key, outcome, rent, all_in, travel, crime, settings, at))


def verdicts_since(chat_id, days, at=None, settings=None):
    since = ((dt.datetime.fromisoformat(at) if at else dt.datetime.now(dt.timezone.utc))
             - dt.timedelta(days=days)).isoformat(timespec="seconds")
    with _db() as db:
        sql = "SELECT * FROM verdicts WHERE chat_id = ? AND updated_at >= ?"
        params = [str(chat_id), since]
        if settings is not None:
            sql += " AND settings = ?"
            params.append(settings)
        return _rows(db, sql, *params)


def pending(chat_id, sources, settings, limit, skip=(), *, drainage=False, at=None, exclude_intents=False):
    """Due work only. Drainage ignores classified retries/intents, NOT unattempted inventory.

    Baseline membership and live intent are durable per advert, so global go-live need not wait for
    a failing advert or for delivery quota. Every active lookup failure eventually becomes due again.
    """
    marks = ",".join("?" for _ in sources)
    at = at or now()
    with _db() as db:
        rows = _rows(db, f"SELECT a.data,a.key,v.outcome,v.settings AS verdict_settings FROM adverts a "
                         f"LEFT JOIN verdicts v ON v.key=a.key AND v.chat_id=? "
                         f"LEFT JOIN advert_work w ON w.key=a.key AND w.chat_id=? "
                         f"WHERE a.status='active' AND a.source IN ({marks}) AND "
                         f"(v.key IS NULL OR v.settings IS NOT ? OR v.outcome IN {RETRY}) "
                         f"ORDER BY CASE WHEN v.outcome='unsent' AND w.notify=1 THEN 0 "
                         f"WHEN v.key IS NULL OR v.settings IS NOT ? THEN 1 ELSE 2 END, "
                         f"w.updated_at,a.first_seen DESC,a.key", str(chat_id), str(chat_id), *sources, settings, settings)
        selected = []
        for row in rows:
            if row["key"] in skip:
                continue
            context = _context(settings, row["data"])
            job = _job(db, chat_id, row["key"])
            classified = job is not None and job["context"] == context
            if drainage and classified and row["verdict_settings"] == settings and row["outcome"] in (
                    "enrichment", "unsent", "deferred", "enrichment_failed"):
                continue
            if _waiting(job, context, at) or (exclude_intents and job and job["notify"]):
                continue
            selected.append(restore(json.loads(row["data"])))
            if len(selected) >= limit:
                break
    return selected


def matched_listings(chat_id, sources, exclude=()):
    marks = ",".join("?" for _ in sources)
    with _db() as db:
        rows = _rows(db, f"SELECT a.data, a.key FROM adverts a JOIN verdicts v ON v.key = a.key AND v.chat_id = ?"
                         f" WHERE v.outcome IN ('matched', 'unsent') AND a.status = 'active' AND a.source IN ({marks})"
                         f" ORDER BY a.key", str(chat_id), *sources)
    return [restore(json.loads(r["data"])) for r in rows if r["key"] not in exclude]


# ---------- sweeps ----------

def start_sweep(source, scope, state, at=None):
    with _db() as db:
        return db.execute("INSERT INTO sweeps (source, started_at, scope, state) VALUES (?, ?, ?, ?)",
                          (source, at or now(), json.dumps(scope, sort_keys=True),
                           json.dumps(state, sort_keys=True))).lastrowid


def _sweep(row):
    return {**row, "scope": json.loads(row["scope"]), "state": json.loads(row["state"]),
            "complete": bool(row["complete"])}


def running_sweep(source):
    with _db() as db:
        rows = _rows(db, "SELECT * FROM sweeps WHERE source = ? AND finished_at IS NULL ORDER BY id DESC LIMIT 1",
                     source)
    return _sweep(rows[0]) if rows else None


def last_sweep(source, complete=None):
    """The latest finished sweep (complete=True: the latest complete one)."""
    sql = "SELECT * FROM sweeps WHERE source = ? AND finished_at IS NOT NULL"
    if complete is not None:
        sql += f" AND complete = {int(bool(complete))}"
    with _db() as db:
        rows = _rows(db, sql + " ORDER BY id DESC LIMIT 1", source)
    return _sweep(rows[0]) if rows else None


def save_sweep(sweep_id, state, requests):
    with _db() as db:
        db.execute("UPDATE sweeps SET state = ?, requests = ? WHERE id = ?",
                   (json.dumps(state, sort_keys=True), requests, sweep_id))


def reserve_request(sweep_id, state, maximum):
    """Charge an attempt BEFORE network I/O. Hard exits may replay a page, never free its cost."""
    with _db() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT requests, finished_at FROM sweeps WHERE id = ?", (sweep_id,)).fetchone()
        if row["finished_at"] is not None or row["requests"] >= maximum:
            return None
        requests = row["requests"] + 1
        db.execute("UPDATE sweeps SET requests = ?, state = ? WHERE id = ?",
                   (requests, json.dumps(state, sort_keys=True), sweep_id))
        return requests


def pending_unseen(chat_id):
    with _db() as db:
        return _rows(db, "SELECT e.*, a.status FROM unseen_events e JOIN adverts a ON a.key = e.key "
                     "LEFT JOIN unseen_deliveries d ON d.event_id = e.id AND d.chat_id = ? "
                     "WHERE d.event_id IS NULL AND NOT EXISTS (SELECT 1 FROM unseen_events newer "
                     "WHERE newer.key = e.key AND newer.id > e.id) ORDER BY e.id", str(chat_id))


def acknowledge_unseen(chat_id, event_id):
    with _db() as db:
        db.execute("INSERT OR IGNORE INTO unseen_deliveries VALUES (?, ?)", (str(chat_id), event_id))


def _in_scope(row, scope):
    centre_lat, centre_lng = scope["centre"]
    km = math.hypot((row["lat"] - centre_lat) * 111.2,
                    (row["lng"] - centre_lng) * 111.2 * math.cos(math.radians(row["lat"])))
    return (scope["min_rent"] <= row["rent"] <= scope["max_rent"] and row["bedrooms"] >= scope["min_beds"]
            and km <= scope["miles"] * 1.609344)


def finish_sweep(sweep_id, complete, note="", at=None):
    """Close a sweep. Only a complete one counts misses; adverts in its scope that no sighting
    reached for UNSEEN_AFTER complete sweeps in a row become "unseen" and return here. Absence is the only
    evidence: the advert may have been let, withdrawn, or re-priced above the swept range, so it is
    never called "gone"."""
    at = at or now()
    unseen = []
    with _db() as db:
        row = _rows(db, "SELECT * FROM sweeps WHERE id = ?", sweep_id)[0]
        db.execute("UPDATE sweeps SET finished_at = ?, complete = ?, note = ? WHERE id = ?",
                   (at, int(complete), note, sweep_id))
        if complete:
            scope = json.loads(row["scope"])
            # Not seen by this sweep, and no head poll showed it while the sweep ran either
            for advert in _rows(db, "SELECT * FROM adverts WHERE source = ? AND status = 'active'"
                                    " AND (last_sweep_id IS NOT ? OR last_sweep_id IS NULL) AND last_seen < ?",
                                row["source"], sweep_id, row["started_at"]):
                if advert["lat"] is None or advert["lng"] is None or advert["rent"] is None \
                        or not _in_scope(advert, scope):
                    continue
                missed = advert["missed"] + 1
                if missed >= UNSEEN_AFTER:
                    db.execute("UPDATE adverts SET missed = ?, status = 'unseen' WHERE key = ?", (missed, advert["key"]))
                    db.execute("INSERT INTO observations (key, at, rent, status) VALUES (?, ?, ?, 'unseen')",
                               (advert["key"], at, advert["rent"]))
                    db.execute("INSERT OR IGNORE INTO unseen_events (sweep_id, key) VALUES (?, ?)",
                               (sweep_id, advert["key"]))
                    unseen.append(advert["key"])
                else:
                    db.execute("UPDATE adverts SET missed = ? WHERE key = ?", (missed, advert["key"]))
    return unseen


def recall(sweep):
    """Share of the adverts a sweep found that the head poll had already shown (None if none)."""
    seen = sweep["adverts_seen"]
    return None if not seen else 1 - sweep["unseen_by_head"] / seen


def counts():
    with _db() as db:
        return {r["status"]: r["n"] for r in _rows(db, "SELECT status, COUNT(*) AS n FROM adverts GROUP BY status")}
