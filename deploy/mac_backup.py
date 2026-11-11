"""Streaming state snapshot helper for deploy/mac-pull-backup.sh."""

import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import sys
import tarfile
import tempfile
import time


STATE = Path("/var/lib/pisobot")
MAX_ARCHIVE_BYTES = 8 * 1024**3
IGNORED_SUFFIXES = (".lock", ".pending", "-wal", "-shm", "-journal")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def ignored(path):
    return (path.name.endswith(IGNORED_SUFFIXES)
            or path.name in (".import-in-progress", "HANDED-OFF-TO-PI")
            or re.fullmatch(r"tmp[a-z0-9_]{8}", path.name) is not None)


def data_files(root):
    found = []
    for path in sorted(root.rglob("*")):
        require(not path.is_symlink(), "State contains a symlink")
        if path.is_dir():
            continue
        require(path.is_file(), "State contains a special file")
        if not ignored(path):
            found.append(path)
    return found


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def database_rows(path):
    with contextlib.closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        require(db.execute("PRAGMA integrity_check").fetchall() == [("ok",)],
                "SQLite integrity check failed")
        names = [row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )]
        rows = {}
        for name in names:
            quoted = '"' + name.replace('"', '""') + '"'
            rows[name] = db.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0]
        return rows


def snapshot_state(source, destination):
    source = Path(source)
    destination = Path(destination)
    require(source.is_dir() and not source.is_symlink(), "State directory is missing or unsafe")
    files = data_files(source)
    require((source / "pisobot.db").is_file(), "Primary SQLite database is missing")
    require((source / "seen.json").is_file(), "Primary seen.json is missing")
    require(any(path.suffix == ".db" for path in files), "No SQLite databases found")
    destination.mkdir(mode=0o700)
    state_copy = destination / "state"
    state_copy.mkdir(mode=0o700)
    for path in files:
        relative = path.relative_to(source)
        target = state_copy / relative
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.suffix == ".db":
            deadline = time.monotonic() + 900

            def progress(_status, _remaining, _total):
                require(time.monotonic() < deadline, "SQLite online backup timed out")

            try:
                with contextlib.closing(sqlite3.connect(
                    path.as_uri() + "?mode=ro", uri=True, timeout=5
                )) as original, contextlib.closing(sqlite3.connect(target)) as backup:
                    original.backup(backup, pages=256, progress=progress, sleep=0.1)
                    backup.execute("PRAGMA journal_mode=DELETE")
            except sqlite3.Error:
                target.unlink(missing_ok=True)
                raise
        else:
            with path.open("rb") as original, target.open("xb") as backup:
                shutil.copyfileobj(original, backup)
            if path.suffix == ".json":
                with target.open(encoding="utf-8") as copied:
                    json.load(copied)
        target.chmod(0o600)

    database_counts = {
        path.relative_to(state_copy).as_posix(): database_rows(path)
        for path in sorted(state_copy.rglob("*.db"))
    }
    manifest = {
        "format": 1,
        "created_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "files": {
            path.relative_to(destination).as_posix(): digest(path)
            for path in sorted((destination / "state").rglob("*")) if path.is_file()
        },
        "database_rows": database_counts,
    }
    (destination / "manifest.json").write_text(json.dumps(manifest, sort_keys=True) + "\n")
    (destination / "manifest.json").chmod(0o600)
    return manifest


def write_tar(snapshot, output):
    with tarfile.open(fileobj=output, mode="w|") as archive:
        for path in sorted(Path(snapshot).rglob("*")):
            if path.is_file():
                archive.add(path, arcname=path.relative_to(snapshot).as_posix(), recursive=False)


def verify_tar(stream, destination):
    destination = Path(destination)
    destination.mkdir(mode=0o700)
    names = set()
    total = 0
    with tarfile.open(fileobj=stream, mode="r|") as archive:
        for member in archive:
            path = PurePosixPath(member.name)
            require(not path.is_absolute() and ".." not in path.parts
                    and path.as_posix() == member.name and path.parts,
                    "Unsafe archive path")
            require(member.isfile(), "Archive contains a non-file")
            require(member.name == "manifest.json" or path.parts[0] == "state",
                    "Unknown archive entry")
            require(member.name not in names, "Duplicate archive entry")
            total += member.size
            require(0 <= member.size and total <= MAX_ARCHIVE_BYTES, "Archive exceeds size limit")
            names.add(member.name)
            target = destination.joinpath(*path.parts)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with archive.extractfile(member) as original, target.open("xb") as copied:
                shutil.copyfileobj(original, copied)
            target.chmod(0o600)

    manifest_path = destination / "manifest.json"
    require(manifest_path.is_file(), "Backup manifest is missing")
    with manifest_path.open(encoding="utf-8") as source:
        manifest = json.load(source)
    require(manifest.get("format") == 1, "Unsupported backup format")
    actual = {
        path.relative_to(destination).as_posix(): digest(path)
        for path in sorted((destination / "state").rglob("*")) if path.is_file()
    }
    require(manifest.get("files") == actual, "Backup file hashes do not match")
    require("state/pisobot.db" in actual and "state/seen.json" in actual,
            "Backup is missing required state")

    databases = {}
    json_files = 0
    for path in sorted((destination / "state").rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(destination / "state").as_posix()
        if path.suffix == ".db":
            databases[relative] = database_rows(path)
        elif path.suffix == ".json":
            with path.open(encoding="utf-8") as source:
                json.load(source)
            json_files += 1
    require(manifest.get("database_rows") == databases, "SQLite row counts do not match")
    return len(databases), sum(sum(rows.values()) for rows in databases.values()), json_files


def prune(directory, retention_days):
    directory = Path(directory)
    require(directory.is_dir() and not directory.is_symlink(), "Backup directory is unsafe")
    require(1 <= retention_days <= 3650, "Retention must be between 1 and 3650 days")
    cutoff = time.time() - retention_days * 86400
    pattern = re.compile(r"pisobot-\d{8}T\d{6}Z\.tar\.gz\.age")
    removed = 0
    for path in directory.iterdir():
        if (pattern.fullmatch(path.name) and not path.is_symlink() and path.is_file()
                and path.stat().st_mtime < cutoff):
            path.unlink()
            checksum = Path(f"{path}.sha256")
            if checksum.is_file() and not checksum.is_symlink():
                checksum.unlink()
            removed += 1
    return removed


def main(args):
    if args == ["export"]:
        with tempfile.TemporaryDirectory(prefix="pisobot-mac-backup-") as work:
            os.chmod(work, 0o700)
            snapshot_state(STATE, Path(work) / "snapshot")
            write_tar(Path(work) / "snapshot", sys.stdout.buffer)
    elif len(args) == 2 and args[0] == "verify":
        databases, rows, json_files = verify_tar(sys.stdin.buffer, args[1])
        print(f"RESTORE VERIFY OK: {databases} SQLite databases, {rows} rows, {json_files} JSON files")
    elif len(args) == 3 and args[0] == "prune" and args[2].isdigit():
        print(f"Removed {prune(args[1], int(args[2]))} expired backup(s)")
    else:
        raise ValueError("Usage: mac_backup.py export | verify PRIVATE_DIR | prune BACKUP_DIR DAYS")


if __name__ == "__main__":
    os.umask(0o077)
    try:
        main(sys.argv[1:])
    except (OSError, ValueError, KeyError, sqlite3.Error, tarfile.TarError) as error:
        if type(error) is ValueError:
            print(f"ERROR: {error}", file=sys.stderr)
        else:
            print(f"ERROR: backup helper failed ({type(error).__name__})", file=sys.stderr)
        sys.exit(1)
