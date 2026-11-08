"""Local SQLite storage using the Worker's schema and one connection per query.

STORAGE=local enables this backend through d1; PISOBOT_STATE_DIR defaults to
the repository's state directory. Connections commit or roll back and always close.
"""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import time

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "worker" / "schema.sql"
BUSY_TIMEOUT_MS = 30_000
_COLUMNS = ("sid", "key", "lat", "lng", "url", "address", "cost")
_UPDATE = ", ".join(f"{c} = excluded.{c}" for c in _COLUMNS[1:])
_PUBLISH_SQL = (
    f"INSERT INTO listings ({', '.join(_COLUMNS)}) SELECT "
    + ", ".join(f"json_extract(value, '$.{c}')" for c in _COLUMNS)
    + f" FROM json_each(?) WHERE true ON CONFLICT(sid) DO UPDATE SET {_UPDATE}"
)


def storage():
    """Only an unset STORAGE selects cloud implicitly; reject invalid settings."""
    value = os.environ.get("STORAGE", "cloud")
    if value not in ("cloud", "local"):
        raise ValueError(f"Invalid STORAGE {value!r}: expected 'cloud' or 'local'")
    return value


def state_dir():
    return Path(os.environ.get("PISOBOT_STATE_DIR") or ROOT / "state")


def database_path():
    return state_dir() / "pisobot.db"


def _enable_wal(db):
    # Competing first connections can get SQLITE_BUSY from journal_mode without
    # SQLite invoking its busy handler. Retry only this initialization step.
    deadline = time.monotonic() + BUSY_TIMEOUT_MS / 1000
    while True:
        try:
            db.execute("PRAGMA journal_mode = WAL")
            return
        except sqlite3.OperationalError as error:
            code = getattr(error, "sqlite_errorcode", 0)
            if code & 0xFF != sqlite3.SQLITE_BUSY or time.monotonic() >= deadline:
                raise
            time.sleep(0.01)


def _execute(db, sql, params):
    try:
        return [dict(row) for row in db.execute(sql, params)]
    except sqlite3.OperationalError as error:
        # Older SQLite builds can lack JSON1. Only emulate prefs.publish's exact
        # statement, never retry unrelated SQL errors or arbitrary JSON queries.
        if str(error).lower() not in (
            "no such table: json_each", "no such function: json_each",
            "no such function: json_extract",
        ) or sql != _PUBLISH_SQL:
            raise
        rows = json.loads(params[0])
        db.executemany(
            f"INSERT INTO listings ({', '.join(_COLUMNS)}) VALUES "
            f"({', '.join('?' for _ in _COLUMNS)}) "
            f"ON CONFLICT(sid) DO UPDATE SET {_UPDATE}",
            [tuple(row.get(c) for c in _COLUMNS) for row in rows],
        )
        return []


def query(sql, *params):
    """Execute a parameterized statement, returning rows as dictionaries."""
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000)) as db:
        db.row_factory = sqlite3.Row
        db.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        _enable_wal(db)
        db.executescript(SCHEMA.read_text(encoding="utf-8"))
        with db:
            return _execute(db, sql, params)
