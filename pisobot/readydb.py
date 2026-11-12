"""Private readiness state, isolated from the databases used for alert delivery."""
import contextlib
import contextvars
import datetime as dt
import json
import os
import sqlite3
import stat

from . import localdb, tracker, users

_ACTIVE = contextvars.ContextVar("pisobot_ready_database", default=None)
# Pre-sidecar draft builds (1201e1a, 81a2320) were never deployed. Their primary-database rows are
# not imported or replayed: readiness refuses to run until an operator removes them while stopped.
_LEGACY_TITLE = "%Application readiness</b>%"
EMPLOYMENT_LENGTH_FOR = "profile.ready_employment_length_for"
_TENURE_STALE = "readiness-tenure-stale"
_DDL = """
CREATE TABLE IF NOT EXISTS renter (name TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS handled (update_id INTEGER PRIMARY KEY, outbox TEXT NOT NULL, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS origin (update_id INTEGER PRIMARY KEY, kind TEXT NOT NULL);
"""


class LegacyData(RuntimeError):
    """Unsupported pre-sidecar readiness data remains in the alert database."""


def path():
    person = users.current()
    directory = person.directory if person is not None and not person.owner else localdb.state_dir()
    return directory / "readiness.db"


def exists():
    return _is_database(path())


def _is_database(pathname):
    try:
        info = pathname.stat()
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(info.st_mode):
        raise OSError("Expected a SQLite database file")
    return True


def _check_isolation(pathname):
    info = pathname.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise OSError("Readiness storage must be a separate regular file")
    for primary in (localdb.database_path(), localdb.state_dir() / "pisobot.db"):
        try:
            other = primary.stat()
        except FileNotFoundError:
            continue
        if (info.st_dev, info.st_ino) == (other.st_dev, other.st_ino):
            raise OSError("Readiness storage must not share an alert database")


def _read(pathname, table, sql, *params):
    if not _is_database(pathname):
        return []
    with contextlib.closing(sqlite3.connect(pathname.resolve().as_uri() + "?mode=ro",
                                           uri=True, timeout=0)) as db:
        db.row_factory = sqlite3.Row
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone():
            return []
        return [dict(row) for row in db.execute(sql, params)]


def _primary_renter(sql, *params):
    rows = _read(localdb.database_path(), "renter", sql, *params)
    return {row["name"]: row["value"] for row in rows}


def primary_value(name):
    return _primary_renter("SELECT name, value FROM renter WHERE name = ?", name).get(name)


def legacy_present():
    """Read-only: private answers or results written by a pre-sidecar draft build."""
    primary = localdb.database_path()
    if _read(primary, "renter", "SELECT 1 FROM renter WHERE name GLOB 'profile.ready_*' LIMIT 1"):
        return True
    return bool(_read(primary, "handled", "SELECT 1 FROM handled WHERE outbox LIKE ? LIMIT 1", _LEGACY_TITLE))


def _decode(raw):
    try:
        pending = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return pending if isinstance(pending, dict) else None


def _pending(raw, at=None):
    pending = _decode(raw)
    try:
        if pending and dt.datetime.fromisoformat(pending["until"]) >= (at or tracker.now()):
            return pending
    except (ValueError, KeyError, TypeError):
        pass
    return None


def primary_pending():
    return _pending(primary_value("_await"))


def primary_expected():
    pending = primary_pending()
    return pending if pending and pending.get("kind") != "done" else None


def primary_outbox(update_id):
    """Read-only: what the alert database's journal still has to send for an update, if any."""
    rows = _read(localdb.database_path(), "handled",
                 "SELECT outbox FROM handled WHERE update_id = ?", update_id)
    if not rows:
        return None
    outbox = json.loads(rows[0]["outbox"])
    if not isinstance(outbox, list):
        raise ValueError("Invalid primary outbox")
    return outbox


def primary_consumed(update_id):
    """Read-only: a disabled run took this update as the answer to a core prompt."""
    pending = _decode(primary_value("_await"))
    return bool(pending and pending.get("kind") == "done" and pending.get("update") == update_id)


def _peek(sql, *params):
    pathname = path()
    if not _is_database(pathname):
        return []
    _check_isolation(pathname)
    return _read(pathname, "renter", sql, *params)


def peek_pending():
    rows = _peek("SELECT value FROM renter WHERE name = '_await'")
    return _pending(rows[0]["value"]) if rows else None


def peek_handled(update_id):
    pathname = path()
    if not _is_database(pathname):
        return None
    _check_isolation(pathname)
    rows = _read(pathname, "handled", "SELECT outbox FROM handled WHERE update_id = ?", update_id)
    return json.loads(rows[0]["outbox"]) if rows else None


@contextlib.contextmanager
def transaction():
    pathname = path()
    active = _ACTIVE.get()
    if active is not None and active[0] == pathname:
        db = active[1]
        db.execute("SAVEPOINT readiness_nested")
        try:
            yield db
        except BaseException:
            db.execute("ROLLBACK TO readiness_nested")
            db.execute("RELEASE readiness_nested")
            raise
        else:
            db.execute("RELEASE readiness_nested")
        return
    if legacy_present():
        raise LegacyData("Remove pre-sidecar readiness data before enabling the pack")
    pathname.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(pathname, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        pass
    else:
        os.close(fd)
    _check_isolation(pathname)
    stale = _tenure_marker(pathname)
    with contextlib.closing(sqlite3.connect(pathname, timeout=0, isolation_level=None)) as db:
        db.row_factory = sqlite3.Row
        # Cleared answers must not survive in free pages of the sidecar file.
        db.execute("PRAGMA secure_delete = ON")
        db.executescript(_DDL)
        db.execute("BEGIN IMMEDIATE")
        token = _ACTIVE.set((pathname, db))
        try:
            invalidated = stale.exists()
            if invalidated:
                db.execute("DELETE FROM renter WHERE name = ?", (EMPLOYMENT_LENGTH_FOR,))
            yield db
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
        finally:
            _ACTIVE.reset(token)
    if invalidated:
        try:
            stale.unlink()
        except OSError:
            # Left in place, it only asks for tenure to be confirmed again.
            pass


def query(sql, *params):
    with transaction() as db:
        return [dict(row) for row in db.execute(sql, params)]


def value(name, default=None):
    rows = query("SELECT value FROM renter WHERE name = ?", name)
    return rows[0]["value"] if rows else default


def put(name, text):
    query("INSERT INTO renter (name, value) VALUES (?, ?) "
          "ON CONFLICT(name) DO UPDATE SET value = excluded.value", name, str(text))


def drop(name):
    query("DELETE FROM renter WHERE name = ?", name)


def profile(names):
    """Read-only renter profile facts; pets and references are only ever edited with /profile."""
    keys = [f"profile.{name}" for name in names]
    facts = _primary_renter(
        "SELECT name, value FROM renter WHERE name IN (" + ", ".join("?" for _ in keys) + ")", *keys)
    return {name: facts.get(f"profile.{name}") for name in names}


def _tenure_marker(pathname):
    return pathname.with_name(_TENURE_STALE)


def invalidate_tenure():
    """Forget which job a tenure answer was confirmed for. If the sidecar cannot be written, a
    marker file makes its next transaction forget it instead; raises only if both fail."""
    try:
        if exists():
            drop(EMPLOYMENT_LENGTH_FOR)
        return
    except Exception:
        pathname = path()
    pathname.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(_tenure_marker(pathname), os.O_CREAT | os.O_WRONLY, 0o600)
    os.close(fd)


def advert(key):
    primary = localdb.state_dir() / "pisobot.db"
    rows = _read(primary, "adverts", "SELECT data FROM adverts WHERE key = ?", key)
    if not rows:
        return {}
    data = json.loads(rows[0]["data"])
    return data if isinstance(data, dict) else {}


def listing(sid):
    rows = _read(localdb.database_path(), "listings", "SELECT * FROM listings WHERE sid = ?", sid)
    return rows[0] if rows else None


def expect(kind, key=None, at=None, update=None, nonce=None, ask=None):
    put("_await", tracker._expectation(kind, key, at, update, nonce, ask))


def expected(at=None):
    raw = value("_await")
    pending = _pending(raw, at)
    if raw and not pending:
        drop("_await")
    return pending if pending and pending.get("kind") != "done" else None


def applied(update_id):
    pending = _pending(value("_await"))
    return pending if update_id is not None and pending and pending.get("update") == update_id else None


def bind(nonce, message_id):
    with transaction():
        pending = _pending(value("_await"))
        if pending and nonce and pending.get("nonce") == nonce:
            pending["message_id"] = message_id
            put("_await", json.dumps(pending))


def done(update=None):
    if update is None:
        drop("_await")
    else:
        until = tracker.now() + dt.timedelta(minutes=tracker.AWAIT_MINUTES)
        put("_await", json.dumps({"kind": "done", "key": None, "until": tracker._iso(until),
                                  "update": update, "nonce": None, "message_id": None}))


def cancel_pending():
    if exists():
        with transaction():
            pending = expected()
            if pending and pending.get("kind") == "ready":
                drop("_await")


def handled(update_id):
    found = query("SELECT outbox FROM handled WHERE update_id = ?", int(update_id))
    return json.loads(found[0]["outbox"]) if found else None


def record(update_id, outbox, at=None, kind=None):
    at = at or tracker.now()
    with transaction() as db:
        tracker._record(db, update_id, outbox, tracker._iso(at))
        if kind is not None:
            db.execute("INSERT OR IGNORE INTO origin (update_id, kind) VALUES (?, ?)", (int(update_id), kind))
        cutoff = tracker._iso(at - dt.timedelta(days=tracker.HANDLED_DAYS))
        db.execute("DELETE FROM origin WHERE update_id IN (SELECT update_id FROM handled WHERE at < ?)", (cutoff,))
        db.execute("DELETE FROM handled WHERE at < ?", (cutoff,))


def stranded(before):
    """Read-only: unsent outboxes of updates the poller has already acknowledged."""
    pathname = path()
    if not _is_database(pathname):
        return []
    _check_isolation(pathname)
    rows = _read(pathname, "handled", "SELECT update_id, outbox FROM handled "
                 "WHERE update_id < ? AND outbox != '[]' ORDER BY update_id", int(before))
    kinds = {row["update_id"]: row["kind"] for row in
             _read(pathname, "origin", "SELECT update_id, kind FROM origin WHERE update_id < ?", int(before))}
    return [(row["update_id"], json.loads(row["outbox"]), kinds.get(row["update_id"])) for row in rows]
