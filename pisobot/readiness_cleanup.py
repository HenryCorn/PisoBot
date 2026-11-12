"""Remove what pre-sidecar readiness test builds (1201e1a, 81a2320) left in the alert databases.

Run once, with the bot and watcher stopped, before enabling READY_PACK:

    python -m pisobot.readiness_cleanup [--dry-run] [--blank-unacknowledged]

Matched by provenance the release never produces: renter keys 'profile.ready_*' and a pending
question of kind 'ready'. Their old replies cannot be told apart from release replies, so every
journal row keeps its update id (an update is never applied twice) and only loses its unsent replies:
below the poller's offset those are never sent again, so dropping them changes nothing else. Rows at
or above the offset could still be replayed; they are listed and left alone unless the owner passes
--blank-unacknowledged. Never imported by the bot or the watcher.
"""
import argparse
import contextlib
import sqlite3
import sys

from . import localdb

OFFSET_KEY = "telegram_update_offset"


def databases():
    root = localdb.state_dir()
    found = [root / "pisobot.db", *sorted((root / "users").glob("*/pisobot.db"))]
    return [path for path in found if path.is_file()]


def _table(db, name):
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone())


def offset(owner_db):
    with contextlib.closing(sqlite3.connect(owner_db.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        if not _table(db, "meta"):
            return None
        row = db.execute("SELECT value FROM meta WHERE name = ?", (OFFSET_KEY,)).fetchone()
    return int(row[0]) if row else None


def survey(db, acknowledged):
    keys = [row[0] for row in db.execute(
        "SELECT name FROM renter WHERE name GLOB 'profile.ready_*' ORDER BY name")] if _table(db, "renter") else []
    prompt = bool(_table(db, "renter") and db.execute(
        "SELECT 1 FROM renter WHERE name = '_await' AND json_valid(value) "
        "AND json_extract(value, '$.kind') = 'ready'").fetchone())
    done, pending = [], []
    if _table(db, "handled"):
        for (update_id,) in db.execute("SELECT update_id FROM handled WHERE outbox != '[]' ORDER BY update_id"):
            (done if acknowledged is not None and update_id < acknowledged else pending).append(update_id)
    return keys, prompt, done, pending


def clean(db, acknowledged, blank_unacknowledged):
    db.execute("DELETE FROM renter WHERE name GLOB 'profile.ready_*'")
    db.execute("DELETE FROM renter WHERE name = '_await' AND json_valid(value) "
               "AND json_extract(value, '$.kind') = 'ready'")
    if _table(db, "handled"):
        if blank_unacknowledged:
            db.execute("UPDATE handled SET outbox = '[]' WHERE outbox != '[]'")
        elif acknowledged is not None:
            db.execute("UPDATE handled SET outbox = '[]' WHERE outbox != '[]' AND update_id < ?", (acknowledged,))


def main(argv=None, out=sys.stdout):
    parser = argparse.ArgumentParser(prog="python -m pisobot.readiness_cleanup", description=__doc__.split("\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="only report what would change")
    parser.add_argument("--blank-unacknowledged", action="store_true",
                        help="also drop unsent replies the poller could still replay (owner decision)")
    args = parser.parse_args(argv)
    paths = databases()
    if not paths:
        print("No alert databases found under", localdb.state_dir(), file=out)
        return 0
    owner = localdb.state_dir() / "pisobot.db"
    acknowledged = offset(owner) if owner.is_file() else None
    blocked = False
    for path in paths:
        with contextlib.closing(sqlite3.connect(path, timeout=0, isolation_level=None)) as db:
            keys, prompt, done, pending = survey(db, acknowledged)
            print(f"{path}: {len(keys)} readiness answer(s), {'a' if prompt else 'no'} readiness question, "
                  f"{len(done)} acknowledged update(s) with unsent replies, "
                  f"{len(pending)} unacknowledged: {pending or 'none'}", file=out)
            if pending and not args.blank_unacknowledged:
                blocked = True
            if args.dry_run or not (keys or prompt or done or (pending and args.blank_unacknowledged)):
                continue
            db.execute("PRAGMA secure_delete = ON")
            db.execute("BEGIN IMMEDIATE")
            try:
                clean(db, acknowledged, args.blank_unacknowledged)
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
            db.execute("VACUUM")
    if blocked:
        print("Unacknowledged updates still have unsent replies; they may be release replies the bot "
              "will send when it restarts, or test-build readiness results. Decide, then rerun with "
              "--blank-unacknowledged to drop them.", file=out)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
