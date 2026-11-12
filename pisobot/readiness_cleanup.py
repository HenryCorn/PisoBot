"""Safely remove legacy readiness rows and, explicitly, readiness sidecars.

Run with the bot and watcher stopped. This tool is intentionally not imported by the bot.
It refuses symlinks, hard-linked database files, and paths outside the state directory.
"""
import argparse
import contextlib
import sqlite3
import stat
import sys

from . import localdb


def _root():
    root = localdb.state_dir()
    try:
        info = root.lstat()
    except FileNotFoundError:
        return root, None
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError(f"State path is not a real directory: {root}")
    return root, root.resolve()


def _checked(path, resolved_root):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError(f"Refusing non-regular, linked, or symlinked database: {path}")
    if path.resolve().parent != path.parent.resolve() or not path.resolve().is_relative_to(resolved_root):
        raise ValueError(f"Refusing database outside state directory: {path}")
    return info


def _person_directories(root, resolved_root):
    directory = root / "users"
    try:
        info = directory.lstat()
    except FileNotFoundError:
        return []
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError(f"Refusing non-directory or symlink: {directory}")
    people = []
    for path in sorted(directory.iterdir()):
        info = path.lstat()
        if stat.S_ISREG(info.st_mode):
            continue
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError(f"Refusing non-directory or symlink: {path}")
        if not path.resolve().is_relative_to(resolved_root):
            raise ValueError(f"Refusing directory outside state directory: {path}")
        people.append(path)
    return people


def _inventory():
    root, resolved_root = _root()
    if resolved_root is None:
        return [], []
    people = _person_directories(root, resolved_root)
    alert_candidates = [root / "pisobot.db", *(person / "pisobot.db" for person in people)]
    sidecar_candidates = [root / "readiness.db", *(person / "readiness.db" for person in people)]
    # Preflight the complete inventory before opening or modifying any database.
    alerts = [(path, _checked(path, resolved_root)) for path in alert_candidates]
    sidecars = [(path, _checked(path, resolved_root)) for path in sidecar_candidates]
    for path, _ in (*alerts, *sidecars):
        for suffix in ("-wal", "-shm", "-journal"):
            _checked(path.with_name(path.name + suffix), resolved_root)
    return ([path for path, info in alerts if info],
            [path for path, info in sidecars if info])


def _table(db, name):
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone())


def _journal_count(db):
    if not _table(db, "handled"):
        return 0
    return db.execute("SELECT count(*) FROM handled WHERE outbox != '[]'").fetchone()[0]


def _legacy_data(db):
    keys = [row[0] for row in db.execute(
        "SELECT name FROM renter WHERE name GLOB 'profile.ready_*' ORDER BY name")] if _table(db, "renter") else []
    prompt = bool(_table(db, "renter") and db.execute(
        "SELECT 1 FROM renter WHERE name = '_await' AND json_valid(value) "
        "AND json_extract(value, '$.kind') = 'ready'").fetchone())
    return keys, prompt


def _clean_legacy(db):
    if not _table(db, "renter"):
        return
    db.execute("DELETE FROM renter WHERE name GLOB 'profile.ready_*'")
    db.execute("DELETE FROM renter WHERE name = '_await' AND json_valid(value) "
               "AND json_extract(value, '$.kind') = 'ready'")


def _purge_sidecar(path):
    with contextlib.closing(sqlite3.connect(path, timeout=0, isolation_level=None)) as db:
        db.execute("PRAGMA secure_delete = ON")
        if _table(db, "renter"):
            db.execute("DELETE FROM renter")
        if _table(db, "handled"):
            db.execute("DELETE FROM handled")
        if _table(db, "origin"):
            db.execute("DELETE FROM origin")
        checkpoint = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint and checkpoint[0]:
            raise sqlite3.OperationalError("readiness sidecar has an active SQLite checkpoint")
        db.execute("VACUUM")
    for suffix in ("-wal", "-shm", "-journal"):
        path.with_name(path.name + suffix).unlink(missing_ok=True)
    path.unlink()


def main(argv=None, out=sys.stdout):
    parser = argparse.ArgumentParser(prog="python -m pisobot.readiness_cleanup", description=__doc__.split("\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="only report what would change")
    parser.add_argument("--purge-sidecars", action="store_true",
                        help="also erase readiness.db files and their SQLite sidecars")
    args = parser.parse_args(argv)
    try:
        alerts, sidecars = _inventory()
        reports = []
        for path in alerts:
            with contextlib.closing(sqlite3.connect(path, timeout=0)) as db:
                keys, prompt = _legacy_data(db)
                pending = _journal_count(db)
            reports.append((path, keys, prompt, pending))
        for path, keys, prompt, pending in reports:
            print(f"{path}: {len(keys)} legacy answer(s), {'a' if prompt else 'no'} legacy question, "
                  f"{pending} journal update(s) with pending replies; journal rows are left unchanged",
                  file=out)
        if args.purge_sidecars:
            for path in sidecars:
                print(f"{path}: readiness sidecar will be removed", file=out)
        if args.dry_run:
            return 2 if any(pending for _, _, _, pending in reports) else 0
        for path, keys, prompt, _ in reports:
            with contextlib.closing(sqlite3.connect(path, timeout=0, isolation_level=None)) as db:
                db.execute("PRAGMA secure_delete = ON")
                db.execute("BEGIN IMMEDIATE")
                try:
                    _clean_legacy(db)
                    db.execute("COMMIT")
                except BaseException:
                    db.execute("ROLLBACK")
                    raise
                # Repack even when the live rows are gone: earlier deletions may remain in free pages.
                db.execute("VACUUM")
        if args.purge_sidecars:
            for path in sidecars:
                _purge_sidecar(path)
        return 2 if any(pending for _, _, _, pending in reports) else 0
    except (OSError, sqlite3.Error, ValueError) as error:
        print(f"Refusing or unable to complete readiness cleanup: {error}", file=out)
        return 1


if __name__ == "__main__":
    sys.exit(main())
