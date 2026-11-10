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
import json
import math
import os
import sqlite3

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
RETRY = "('enrichment', 'unsent', 'deferred')"  # failed lookup, durable delivery intent, or deadline yield
UNSEEN_AFTER = 2   # consecutive complete sweeps an in-scope advert may be missing from
ARCHIVE_IMAGES = 16   # photos kept per advert: the card's header plus a "More photos" album (2-10)


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
            data = json.dumps(snapshot(l, max_images=ARCHIVE_IMAGES), sort_keys=True)
            if old is None:
                db.execute("INSERT INTO adverts (key, source, url, address, bedrooms, rent, lat, lng, first_seen,"
                           " last_seen, portal_first_listed, status, head_seen, last_sweep_id, missed, data)"
                           " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, 0, ?)",
                           (l.key, l.source, l.url, l.address, l.bedrooms, l.rent, l.lat, l.lng, at, at,
                            l.first_listed, int(via == "head"), sweep_id if via == "sweep" else None, data))
                db.execute("INSERT INTO observations (key, at, rent, status) VALUES (?, ?, ?, 'active')",
                           (l.key, at, l.rent))
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

def record_verdicts(chat_id, rows, settings, at=None):
    """rows: (key, outcome, rent, all_in, travel_minutes, crime_score). Latest outcome wins."""
    at = at or now()
    with _db() as db:
        db.executemany(
            "INSERT INTO verdicts (chat_id, key, outcome, rent, all_in, travel_minutes, crime_score, settings,"
            " updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(chat_id, key) DO UPDATE SET outcome = excluded.outcome, rent = excluded.rent,"
            " all_in = excluded.all_in, travel_minutes = excluded.travel_minutes,"
            " crime_score = excluded.crime_score, settings = excluded.settings, updated_at = excluded.updated_at",
            [(str(chat_id), key, outcome, rent, all_in, travel, crime, settings, at)
             for key, outcome, rent, all_in, travel, crime in rows])


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


def pending(chat_id, sources, settings, limit, skip=()):
    """Active archived adverts this person's current settings have not judged yet (a failed
    enrichment, or a match whose alert hit MAX_ALERTS_PER_RUN, counts as not judged), newest first,
    as Listing objects."""
    marks = ",".join("?" for _ in sources)
    with _db() as db:
        rows = _rows(db, f"SELECT a.data, a.key FROM adverts a LEFT JOIN verdicts v ON v.key = a.key"
                         f" AND v.chat_id = ? WHERE a.status = 'active' AND a.source IN ({marks})"
                         f" AND (v.key IS NULL OR v.settings IS NOT ? OR v.outcome IN {RETRY})"
                         f" ORDER BY a.first_seen DESC, a.key", str(chat_id), *sources, settings)
    rows = [r for r in rows if r["key"] not in skip]
    return [restore(json.loads(r["data"])) for r in rows[:limit]]


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
