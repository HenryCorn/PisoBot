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
_MIGRATED = "_legacy_migrated"
_SHARED = ("pets", "references")
EMPLOYMENT_LENGTH_FOR = "profile.ready_employment_length_for"
_DDL = """
CREATE TABLE IF NOT EXISTS renter (name TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS handled (update_id INTEGER PRIMARY KEY, outbox TEXT NOT NULL, at TEXT NOT NULL);
"""


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
                                           uri=True, timeout=localdb.BUSY_TIMEOUT_MS / 1000)) as db:
        db.row_factory = sqlite3.Row
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone():
            return []
        return [dict(row) for row in db.execute(sql, params)]


def _primary_renter(sql, *params):
    rows = _read(localdb.database_path(), "renter", sql, *params)
    return {row["name"]: row["value"] for row in rows}


def primary_value(name):
    return _primary_renter("SELECT name, value FROM renter WHERE name = ?", name).get(name)


def _pending(raw, at=None):
    try:
        pending = json.loads(raw)
        if (isinstance(pending, dict) and
                dt.datetime.fromisoformat(pending["until"]) >= (at or tracker.now())):
            return pending
    except (ValueError, KeyError, TypeError):
        pass
    return None


def primary_expected():
    pending = _pending(primary_value("_await"))
    return pending if pending and pending.get("kind") != "done" else None


def primary_completed(update_id):
    return bool(_read(localdb.database_path(), "handled",
                      "SELECT update_id FROM handled WHERE update_id = ? AND outbox = '[]'", update_id))


def _migrate(db):
    if db.execute("SELECT 1 FROM renter WHERE name = ?", (_MIGRATED,)).fetchone():
        return
    legacy = _primary_renter("SELECT name, value FROM renter "
                             "WHERE name GLOB 'profile.ready_*' OR name = '_await'")
    rows = [(name, value) for name, value in legacy.items() if name.startswith("profile.ready_")]
    pending = _pending(legacy.get("_await"))
    if pending and pending.get("kind") == "ready":
        rows.append(("_await", legacy["_await"]))
        update = pending.get("update")
        if type(update) is int:
            handled = _read(localdb.database_path(), "handled",
                            "SELECT update_id, outbox, at FROM handled WHERE update_id = ?", update)
            for row in handled:
                db.execute("INSERT OR IGNORE INTO handled VALUES (?, ?, ?)",
                           (row["update_id"], row["outbox"], row["at"]))
    db.executemany("INSERT OR IGNORE INTO renter (name, value) VALUES (?, ?)", rows)
    db.execute("INSERT INTO renter (name, value) VALUES (?, '1')", (_MIGRATED,))


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
    pathname.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(pathname, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        pass
    else:
        os.close(fd)
    _check_isolation(pathname)
    with contextlib.closing(sqlite3.connect(pathname, timeout=localdb.BUSY_TIMEOUT_MS / 1000,
                                           isolation_level=None)) as db:
        db.row_factory = sqlite3.Row
        db.executescript(_DDL)
        db.execute("BEGIN IMMEDIATE")
        token = _ACTIVE.set((pathname, db))
        try:
            _migrate(db)
            yield db
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
        finally:
            _ACTIVE.reset(token)


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
    keys = [f"profile.{name}" for name in names]
    legacy = _primary_renter(
        "SELECT name, value FROM renter WHERE name IN (" + ", ".join("?" for _ in keys) + ")", *keys)
    facts = {name: legacy.get(f"profile.{name}") for name in names}
    if exists():
        for name in _SHARED:
            raw = value(f"profile.{name}")
            if raw is None:
                continue
            saved = json.loads(raw)
            if not isinstance(saved, dict) or set(saved) != {"base", "value"}:
                raise ValueError("Invalid shared readiness field")
            if saved["base"] == facts[name]:
                facts[name] = saved["value"]
            else:
                drop(f"profile.{name}")
    return facts


def put_shared(name, text):
    base = primary_value(f"profile.{name}")
    put(f"profile.{name}", json.dumps({"base": base, "value": text}))


def clear_shared(name):
    if exists() and name in _SHARED:
        drop(f"profile.{name}")


def invalidate_tenure():
    if exists():
        drop(EMPLOYMENT_LENGTH_FOR)


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


def record(update_id, outbox, at=None):
    at = at or tracker.now()
    with transaction() as db:
        tracker._record(db, update_id, outbox, tracker._iso(at))
        db.execute("DELETE FROM handled WHERE at < ?",
                   (tracker._iso(at - dt.timedelta(days=tracker.HANDLED_DAYS)),))
