"""The optional facts feature's OWN SQLite file (G2/G6): `facts.db` beside the main database, never inside it.

Everything the feature stores lives here: the queue of facts wanted, the replies already sent, certificates and postcode
summaries, comparables, floorplan readings and the usage counters. A lock this file's writers hold can therefore never touch the
main process's receipt or acknowledgement writes, and deleting the one file erases everything the feature has stored.
Importing this module does nothing: no file is created until a store is actually used.
"""
import contextlib
import os
import sqlite3

from . import localdb

NAME = "facts.db"
BUSY_SECONDS = 0.25          # waiting for a lock; a transaction must also stay short (nothing slow happens inside one)


def path():
    return localdb.state_dir() / NAME


def exists():
    return os.path.exists(path())


@contextlib.contextmanager
def connect(ddl=None, db_path=None, create=True):
    """One transaction on facts.db (committed or rolled back, always closed). `ddl` is executed first (CREATE ... IF NOT EXISTS).
    With create=False a missing file raises instead of being created."""
    target = db_path or path()
    if create:
        os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)
    elif not os.path.exists(target):
        raise FileNotFoundError("no facts database")
    with contextlib.closing(sqlite3.connect(target, timeout=BUSY_SECONDS)) as db:
        db.row_factory = sqlite3.Row
        db.execute(f"PRAGMA busy_timeout = {int(BUSY_SECONDS * 1000)}")
        with contextlib.suppress(sqlite3.Error):
            db.execute("PRAGMA journal_mode = WAL")
        if ddl:
            db.executescript(ddl)
        with db:
            yield db


@contextlib.contextmanager
def read_only(db_path):
    """A read-only, query_only connection with a short timeout on some OTHER database (the main or a person's): it takes no
    write lock and raises on any attempt to write. Used for the 'is this person still active / did they hide this home' checks."""
    if not os.path.exists(db_path):
        raise FileNotFoundError(str(db_path))
    from urllib.parse import quote
    with contextlib.closing(sqlite3.connect(f"file:{quote(str(db_path))}?mode=ro", uri=True, timeout=BUSY_SECONDS)) as db:
        db.row_factory = sqlite3.Row
        db.execute(f"PRAGMA busy_timeout = {int(BUSY_SECONDS * 1000)}")
        db.execute("PRAGMA query_only = 1")
        yield db
