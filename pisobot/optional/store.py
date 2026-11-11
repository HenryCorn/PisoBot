"""The optional features' OWN SQLite file (optional.db) and a bounded, read-only window onto the primary database.

What this module guarantees, and what it does not:
  * The optional writer validates its own path before and after SQLite opens it: (1) the state directory must be ours and not writable
    by group or others; (2) the path must be a regular, singly-linked file called optional.db directly inside the real state directory;
    (3) SQLite's reported path must match and its reserved identity row must match that file's device and inode. New files are seeded
    through an exclusive no-follow descriptor before SQLite opens them. Every writer connection also carries an authorizer that denies
    ATTACH/DETACH/PRAGMA/transaction control and any schema other than main/temp. SQL cannot reach another file, and a path that is or
    becomes a link is refused. These path checks are not a sandbox against code running as the same user, which can also replace paths
    or open any file itself; Python's SQLite API does not expose the opened database's file descriptor for an inode check.
  * a write is one small transaction (BEGIN IMMEDIATE ... COMMIT, at most MAX_STATEMENTS statements). A progress handler interrupts long
    computations and the connection is closed on every path. It does NOT make the transaction hard-real-time: a stalled fsync/commit cannot
    be interrupted from here. The protection of the primary database is that the optional writer lives in another file, not a time limit.
  * reads (optional.db and the primary database) are read-only (`mode=ro`, `query_only`), authorized for SELECT only, limited by a statement
    deadline and a row cap, and the connection is closed before the call returns: no snapshot is held across calls.
Importing this module does no work.
"""
import contextlib
import os
import sqlite3
import time
from pathlib import Path
from urllib.parse import quote

NAME = "optional.db"
IDENTITY_TABLE = "_pisobot_optional_identity"
BUSY_SECONDS = 0.25
TX_SECONDS = 0.5
READ_SECONDS = 0.5
MAX_STATEMENTS = 200
MAX_ROWS = 5000

_ALLOWED_WRITE = {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE, sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ,
                  sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_CREATE_INDEX, sqlite3.SQLITE_CREATE_VIEW,
                  sqlite3.SQLITE_DROP_TABLE, sqlite3.SQLITE_DROP_INDEX, sqlite3.SQLITE_DROP_VIEW, sqlite3.SQLITE_ALTER_TABLE,
                  sqlite3.SQLITE_RECURSIVE, sqlite3.SQLITE_REINDEX}
_ALLOWED_READ = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}


def _state():
    from .. import localdb
    return Path(os.path.abspath(localdb.state_dir()))


def path():
    return _state() / NAME


def exists():
    return os.path.lexists(path())


last_error = [""]             # why the last write or read was refused (shown by optional.health(); diagnosis only)


def _private_directory(directory):
    """The state directory must belong to this user and not be writable by anyone else, or another user could swap the file under us."""
    info = os.stat(directory)
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise ValueError("the state directory belongs to another user")
    if info.st_mode & 0o022:
        raise ValueError("the state directory is writable by group or others")


def _plain(target):
    if os.path.lexists(target):
        info = os.lstat(target)
        if os.path.islink(target) or not os.path.isfile(target) or info.st_nlink != 1:
            raise ValueError("optional.db must be a plain, unlinked file")


def _verify_open(db, target):
    """Check SQLite's resolved path and the opened database's identity before any write."""
    expected = os.path.join(os.path.realpath(_state()), NAME)
    held = [row[2] for row in db.execute("PRAGMA database_list") if row[1] == "main"]
    if len(held) != 1 or not held[0] or os.path.realpath(held[0]) != expected or os.path.basename(held[0]) != NAME:
        raise ValueError("the open file is not optional.db")
    _plain(target)
    _plain(expected)
    info = os.stat(target, follow_symlinks=False)
    try:
        rows = db.execute(f"SELECT device, inode FROM {IDENTITY_TABLE}").fetchall()
    except sqlite3.Error as ex:
        raise ValueError("optional.db lacks its identity; refusing the file") from ex
    if rows != [(str(info.st_dev), str(info.st_ino))]:
        raise ValueError("optional.db identity does not match the open file")


def _create(target):
    """Create and seed a new optional database through a no-follow, exclusive file descriptor."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(target, flags, 0o600)
    except FileExistsError:
        return
    try:
        info = os.fstat(fd)
        with contextlib.closing(sqlite3.connect(":memory:")) as seed:
            seed.execute(f"CREATE TABLE {IDENTITY_TABLE} (device TEXT NOT NULL, inode TEXT NOT NULL)")
            seed.execute(f"INSERT INTO {IDENTITY_TABLE} VALUES (?, ?)", (str(info.st_dev), str(info.st_ino)))
            seed.commit()
            data = memoryview(seed.serialize())
        while data:
            written = os.write(fd, data)
            if written <= 0:
                raise OSError("could not initialize optional.db")
            data = data[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def checked_path():
    """The optional database path, after proving it cannot be a primary database. Raises ValueError otherwise."""
    target = path()
    state_real = Path(os.path.realpath(_state()))
    if os.path.isdir(state_real):
        _private_directory(state_real)
    if target.name != NAME:
        raise ValueError("not the optional database")
    _plain(target)
    if Path(os.path.realpath(target)).parent != state_real:
        raise ValueError("optional.db must sit directly in the state directory")
    return target


def _guard(allowed):
    def authorizer(action, arg1, arg2, db_name, source):
        if action not in allowed:
            return sqlite3.SQLITE_DENY
        if db_name not in (None, "main", "temp"):
            return sqlite3.SQLITE_DENY
        if allowed is _ALLOWED_WRITE and action not in _ALLOWED_READ and IDENTITY_TABLE in (arg1, arg2):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    return authorizer


def write(ddl, statements):
    """Run `statements` ([(sql, params), ...], at most MAX_STATEMENTS) in ONE transaction on optional.db, creating the file and applying
    `ddl` (CREATE ... IF NOT EXISTS) first. True when committed; False on contention, interruption, a denied statement or any error
    (nothing half-written, nothing raised). Too many statements is a programming error and raises before any I/O."""
    if len(statements) > MAX_STATEMENTS:
        raise ValueError("an optional transaction must be small")
    try:
        target = checked_path()
        os.makedirs(os.path.dirname(target), exist_ok=True)
        target = checked_path()
        _create(target)
        target = checked_path()
        with contextlib.closing(sqlite3.connect(target, timeout=BUSY_SECONDS, isolation_level=None)) as db:
            _verify_open(db, target)                      # nothing has been written yet; a swapped path is refused here
            db.execute(f"PRAGMA busy_timeout = {int(BUSY_SECONDS * 1000)}")
            with contextlib.suppress(sqlite3.Error):
                db.execute("PRAGMA journal_mode = WAL")
            deadline = time.monotonic() + TX_SECONDS
            db.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 1000)
            try:
                db.execute("BEGIN IMMEDIATE")
                db.set_authorizer(_guard(_ALLOWED_WRITE))
                for stmt in [s for s in (ddl or "").split(";") if s.strip()]:
                    db.execute(stmt)
                for sql, params in statements:
                    db.execute(sql, params)
                db.set_authorizer(None)
                db.execute("COMMIT")
            except Exception:
                db.set_authorizer(None)
                with contextlib.suppress(Exception):
                    db.execute("ROLLBACK")
                last_error[0] = "transaction failed"
                return False
        last_error[0] = ""
        return True
    except Exception as ex:
        last_error[0] = str(ex)[:120] if isinstance(ex, ValueError) else type(ex).__name__
        return False


def _read(target, sql, params, own=False):
    with contextlib.closing(sqlite3.connect(f"file:{quote(str(target))}?mode=ro", uri=True, timeout=BUSY_SECONDS)) as db:
        if own:
            _verify_open(db, target)
        db.row_factory = sqlite3.Row
        db.execute(f"PRAGMA busy_timeout = {int(BUSY_SECONDS * 1000)}")
        db.execute("PRAGMA query_only = 1")
        deadline = time.monotonic() + READ_SECONDS
        db.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 1000)
        db.set_authorizer(_guard(_ALLOWED_READ))
        cursor = db.execute(sql, params)
        rows = [dict(r) for r in cursor.fetchmany(MAX_ROWS + 1)]
        cursor.close()
        if len(rows) > MAX_ROWS:
            raise ValueError("too many rows")
        return rows


def read(sql, params=()):
    """Rows (as dicts) from optional.db, read-only and bounded. [] when the file or table does not exist (nothing is ever created) or on
    any error."""
    try:
        if not exists():
            return []
        return _read(checked_path(), sql, params, own=True)
    except Exception:
        return []


def read_primary(sql, params=(), person=None):
    """Rows (as dicts) from the primary database, or from `person`'s database (a chat id): one SELECT, read-only, authorized for SELECT
    only, at most MAX_ROWS rows, a statement deadline, and the snapshot is closed before returning. [] on any error or a missing file.
    There is no write counterpart."""
    try:
        from .. import localdb, users
        if person is None:
            target = localdb.state_dir() / "pisobot.db"
        else:
            owner = str(person) == os.environ.get("TELEGRAM_CHAT_ID", "").strip()
            target = localdb.state_dir() / "pisobot.db" if owner else users.User(str(person), owner=False).directory / "pisobot.db"
        if not os.path.exists(target):
            return []
        return _read(target, sql, params)
    except Exception:
        return []
