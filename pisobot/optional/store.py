"""The optional features' OWN SQLite file (optional.db) and the read-only window onto the primary database.

  * the file lives beside the main database and is created lazily: only an enabled feature's first write creates it;
  * a write is ONE short transaction (a small batch of statements, nothing else can run inside it: no callbacks, so no HTTP and no sleep)
    that SQLite itself INTERRUPTS if it runs longer than TX_SECONDS, so a lock is never held long: the short busy timeout bounds waiting,
    this bounds holding;
  * optional code has no write access to the primary database at all: read_primary() is read-only (`mode=ro`, `query_only`, short timeout).
Importing this module does no work.
"""
import contextlib
import os
import sqlite3
import time
from urllib.parse import quote

NAME = "optional.db"
BUSY_SECONDS = 0.25        # waiting for a lock
TX_SECONDS = 0.5           # holding one: the transaction is interrupted after this
MAX_STATEMENTS = 200


def path():
    from .. import localdb
    return localdb.state_dir() / NAME


def exists():
    return os.path.exists(path())


def write(ddl, statements):
    """Run `statements` ([(sql, params), ...], at most MAX_STATEMENTS) in ONE transaction on optional.db, creating the file and applying
    `ddl` (CREATE ... IF NOT EXISTS) first. Returns True when committed; False on contention, interruption or any error (nothing is
    left half-written, and nothing is raised). Too many statements is a programming error and raises before any I/O."""
    if len(statements) > MAX_STATEMENTS:
        raise ValueError("an optional transaction must be small")
    try:
        target = path()
        os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)
        with contextlib.closing(sqlite3.connect(target, timeout=BUSY_SECONDS)) as db:
            db.execute(f"PRAGMA busy_timeout = {int(BUSY_SECONDS * 1000)}")
            with contextlib.suppress(sqlite3.Error):
                db.execute("PRAGMA journal_mode = WAL")
            deadline = time.monotonic() + TX_SECONDS
            db.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 1000)
            try:
                db.execute("BEGIN IMMEDIATE")
                if ddl:
                    for stmt in [s for s in ddl.split(";") if s.strip()]:
                        db.execute(stmt)
                for sql, params in statements:
                    db.execute(sql, params)
                db.execute("COMMIT")
            except Exception:
                with contextlib.suppress(Exception):
                    db.execute("ROLLBACK")
                return False
        return True
    except Exception:
        return False


def read(sql, params=()):
    """Rows (as dicts) from optional.db, read-only. [] when the file or the table does not exist (nothing is ever created) or on any error."""
    try:
        if not exists():
            return []
        with contextlib.closing(sqlite3.connect(f"file:{quote(str(path()))}?mode=ro", uri=True, timeout=BUSY_SECONDS)) as db:
            db.row_factory = sqlite3.Row
            db.execute(f"PRAGMA busy_timeout = {int(BUSY_SECONDS * 1000)}")
            db.execute("PRAGMA query_only = 1")
            return [dict(r) for r in db.execute(sql, params)]
    except Exception:
        return []


def read_primary(sql, params=(), person=None):
    """Rows (as dicts) from the primary database, or from `person`'s database (a chat id), READ-ONLY with a short timeout. [] on any
    error or a missing file. There is no write counterpart: optional code cannot write to the primary database."""
    try:
        from .. import localdb, users
        if person is None:
            target = localdb.state_dir() / "pisobot.db"
        else:
            owner = str(person) == os.environ.get("TELEGRAM_CHAT_ID", "").strip()
            target = localdb.state_dir() / "pisobot.db" if owner else users.User(str(person), owner=False).directory / "pisobot.db"
        if not os.path.exists(target):
            return []
        with contextlib.closing(sqlite3.connect(f"file:{quote(str(target))}?mode=ro", uri=True, timeout=BUSY_SECONDS)) as db:
            db.row_factory = sqlite3.Row
            db.execute(f"PRAGMA busy_timeout = {int(BUSY_SECONDS * 1000)}")
            db.execute("PRAGMA query_only = 1")
            return [dict(r) for r in db.execute(sql, params)]
    except Exception:
        return []
