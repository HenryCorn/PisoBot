"""Private implementation helpers for the guarded shell commands; stdlib only."""

import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import sys
import tarfile
import time
import urllib.request


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def private_file(path):
    require(path.is_file() and not path.is_symlink(), "Expected a regular private file")
    require(stat.S_IMODE(path.stat().st_mode) == 0o600, "Private file must have mode 600")
    require(path.stat().st_uid == os.geteuid(), "Private file must belong to the invoking user")


def plain_path(path):
    require(path.is_absolute() and ".." not in path.parts, "Use an absolute, normalized path")
    require(not any(p.is_symlink() for p in (path, *path.parents)), "Symlink paths are not accepted")


def test_paths(root, paths):
    root = Path(root)
    plain_path(root)
    require(root.name.startswith("pisobot-kit-test-"), "Test root must be a dedicated pisobot-kit-test-* directory")
    require(root.is_dir() and root.stat().st_uid == os.geteuid(), "Test root must belong to you")
    require(stat.S_IMODE(root.stat().st_mode) == 0o700, "Test root must have mode 700")
    marker = root / ".fake-state-only"
    require(marker.is_file() and marker.read_text() == "fake-state-only\n", "Missing fake-state marker")
    for path in map(Path, paths):
        plain_path(path)
        require(path != root and root in path.parents, "Test paths must be strictly inside the fake-state root")


HANDOFF = "HANDED-OFF-TO-PI"


def ignored(path):
    # Never part of a snapshot: locks, SQLite sidecars, the handoff marker and the half-written
    # temp files of pisobot.main.save_seen (NamedTemporaryFile: "tmp" + 8 random characters).
    return (path.name.endswith((".lock", ".pending", "-wal", "-shm", "-journal")) or path.name == HANDOFF
            or re.fullmatch(r"tmp[a-z0-9_]{8}", path.name) is not None)


# Entries that do not make a state directory "occupied": any run creates the lock files,
# lost+found belongs to the filesystem and the handoff marker is managed by the kit.
# Everything else is live data that must not be lost.
KEEP = ("watch.lock", "run.lock", ".import-in-progress", HANDOFF, "lost+found")


def files(root):
    found = []
    for path in sorted(root.rglob("*")):
        require(not path.is_symlink(), "State/archive contains a symlink")
        require(path.is_dir() or path.is_file(), "State/archive contains a special file")
        if path.is_file():
            found.append(path)
    return found


def check_db(path):
    with contextlib.closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        require(db.execute("PRAGMA integrity_check").fetchall() == [("ok",)], "SQLite integrity check failed")


def check_state(root):
    require((root / "pisobot.db").is_file(), "Missing primary database")
    require((root / "seen.json").is_file(), "Missing primary seen.json")
    require((root / "cache.json").is_file(), "Missing cache.json")
    databases = 0
    for path in files(root):
        require(not ignored(path), "Snapshot contains a lock, pending file or SQLite sidecar")
        if path.suffix == ".db":
            check_db(path)
            databases += 1
        elif path.suffix == ".json":
            with path.open() as stream:
                json.load(stream)
    return databases


@contextlib.contextmanager
def stopped_locks(root, create=False):
    # These are the same locks used by pisobot.main.run_lock, not PID files. Only an import into a
    # fresh state may create them: an exporter running as root would otherwise leave root-owned
    # lock files that the service user cannot lock.
    with contextlib.ExitStack() as stack:
        for name in ("watch.lock", "run.lock"):
            path = root / name
            require(not path.is_symlink(), "Lock must not be a symlink")
            flags = (os.O_RDWR | os.O_CREAT) if create else os.O_RDONLY
            try:
                descriptor = os.open(path, flags | os.O_NOFOLLOW, 0o600)
            except FileNotFoundError:
                raise ValueError(f"{name} is missing from the state directory and is never created here "
                                 "(it could get the wrong owner). Create it as the service user, then retry.") from None
            stream = stack.enter_context(os.fdopen(descriptor, "r"))
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def snapshot(state, env, dest, revision, host, quiesced, final):
    plain_path(state)
    plain_path(env)
    require(state.is_dir(), "State directory is missing")
    private_file(env)
    require(re.fullmatch(r"[a-f0-9]{40,64}", revision), "Cannot identify application commit")
    require(not final or quiesced, "Final export requires the application locks")
    dest.mkdir(mode=0o700)
    require(state not in dest.parents, "Scratch must be outside the state directory")
    lock = stopped_locks(state) if quiesced else contextlib.nullcontext()
    with lock:
        if final:
            handoff(state, "set")
            persist_files(state / HANDOFF)
        source_files = files(state)
        size = sum(path.stat().st_size for path in source_files)
        require(shutil.disk_usage(dest).free > size * 2 + 16 * 1024**2, "Insufficient snapshot/encryption space")
        (dest / "state").mkdir(mode=0o700)
        for src in source_files:
            if ignored(src):
                continue
            target = dest / "state" / src.relative_to(state)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if src.suffix == ".db":
                deadline = time.monotonic() + 120
                sidecars = [src.with_name(src.name + suffix) for suffix in ("-wal", "-shm")]
                absent_before = [not os.path.lexists(p) for p in sidecars]

                def progress(_status, _remaining, _total):
                    require(time.monotonic() < deadline, "SQLite online backup timed out")

                try:
                    with contextlib.closing(sqlite3.connect(src.as_uri() + "?mode=ro", uri=True)) as source:
                        with contextlib.closing(sqlite3.connect(target)) as backup:
                            source.backup(backup, pages=256, progress=progress, sleep=0.1)
                            backup.execute("PRAGMA journal_mode=DELETE")
                finally:
                    # The read-only open can leave empty -wal/-shm files behind (root-owned on the Pi).
                    # With the service stopped and locked nobody else uses them, so remove only the
                    # ones this open created. A live export never deletes: the service may be using them.
                    if quiesced:
                        for sidecar, absent in zip(sidecars, absent_before):
                            if absent:
                                sidecar.unlink(missing_ok=True)
            else:
                # Open once: cache/seen writers publish complete JSON by atomic rename.
                with src.open("rb") as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output)
            target.chmod(0o600)
        (dest / "env").mkdir(mode=0o700)
        shutil.copyfile(env, dest / "env/pisobot.env")
        (dest / "env/pisobot.env").chmod(0o600)
    count = check_state(dest / "state")
    manifest = {
        "format": 1,
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "revision": revision,
        "source_host": host,
        "quiesced": quiesced,
        "final": final,
        "test_only": bool(os.environ.get("PISOBOT_TEST_ROOT")),
        "files": {p.relative_to(dest).as_posix(): digest(p) for p in files(dest)},
    }
    (dest / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"Snapshot OK: {count} SQLite databases; quiesced={quiesced}; final={final}", file=sys.stderr)


def pack(root):
    with tarfile.open(fileobj=sys.stdout.buffer, mode="w|gz") as archive:
        for path in files(root):
            archive.add(path, arcname=path.relative_to(root).as_posix(), recursive=False)


def unpack(root):
    root.mkdir(mode=0o700)
    total, names = 0, set()
    limit = int(os.environ.get("PISOBOT_MAX_ARCHIVE_BYTES", 1024**3))
    with tarfile.open(fileobj=sys.stdin.buffer, mode="r|gz") as archive:
        for member in archive:
            path = PurePosixPath(member.name)
            require(not path.is_absolute() and ".." not in path.parts, "Unsafe archive path")
            require(path.as_posix() == member.name and path.parts, "Non-canonical archive path")
            require(member.name not in names, "Duplicate archive member")
            require(member.isfile(), "Only regular files are accepted in the archive")
            require(member.name == "manifest.json" or path.parts[0] in ("state", "env"), "Unknown archive member")
            total += member.size
            require(0 <= member.size and total <= limit, "Archive exceeds size limit")
            require(shutil.disk_usage(root).free > member.size + 16 * 1024**2, "Insufficient restore space")
            names.add(member.name)
            target = root.joinpath(*path.parts)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with archive.extractfile(member) as source, target.open("xb") as dest:
                shutil.copyfileobj(source, dest)
            target.chmod(0o600)


def verify(root, mode="", host="", revision=""):
    manifest = json.loads((root / "manifest.json").read_text())
    require(manifest["format"] == 1, "Unsupported snapshot format")
    require(manifest["test_only"] is bool(os.environ.get("PISOBOT_TEST_ROOT")), "Test/production archive mismatch")
    actual = {p.relative_to(root).as_posix(): digest(p) for p in files(root) if p != root / "manifest.json"}
    require(manifest["files"] == actual, "Snapshot manifest/file checksum mismatch")
    require({p.relative_to(root).as_posix() for p in files(root / "env")} == {"env/pisobot.env"},
            "Expected exactly one environment file")
    if mode:
        require(manifest["quiesced"] is True, "Import requires a quiesced snapshot")
        require(mode == "recovery" or manifest["final"] is True, "Cutover requires a final, stopped-source export")
        require(manifest["source_host"] == host, "Source-stopped acknowledgement does not match archive host")
        require(manifest["revision"] == revision, "Application commit differs from snapshot; deploy the source commit first")
    count = check_state(root / "state")
    print(f"RESTORE TEST OK: {count} SQLite databases; all file hashes and JSON verified.")


def occupied(state):
    return any(item.name not in KEEP for item in state.iterdir())


def durable_path(directory, *others):
    plain_path(directory)
    for other in others:
        plain_path(other)
        require(directory != other and other not in directory.parents and directory not in other.parents,
                "The encrypted-copy directory must be separate from the state and scratch directories")


def sync_path(path, directory=False):
    flags = os.O_RDONLY | os.O_NOFOLLOW | (os.O_DIRECTORY if directory else 0)
    descriptor = os.open(path, flags)
    try:
        mode = os.fstat(descriptor).st_mode
        require(stat.S_ISDIR(mode) if directory else stat.S_ISREG(mode), "Unexpected persistence target type")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def persist_files(*paths):
    directories = set()
    for path in paths:
        plain_path(path)
        sync_path(path)
        directories.update(path.parents)
    # Leaf first, through the filesystem root: backup directories may have just been created.
    # Any open/fsync failure is fatal, including unsupported directory synchronization.
    for directory in sorted(directories, key=lambda p: (-len(p.parts), str(p))):
        sync_path(directory, directory=True)


def fingerprint(state):
    # Same names, sizes and mtimes mean nothing was written since the encrypted copy was taken.
    # WAL commits need not touch the main DB; include the WAL even though snapshots omit it.
    plain_path(state)
    lines = [f"{p.relative_to(state).as_posix()} {p.stat().st_size} {p.stat().st_mtime_ns}"
             for p in files(state) if not ignored(p) or p.name.endswith("-wal")]
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


def job_dir(work):
    plain_path(work)
    require(work.is_dir() and work.name.startswith("job."), "Unsafe cleanup target")
    return work


def remove_entry(path):
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def plain_names(names):
    for name in names:
        require(name and "/" not in name and name not in (".", ".."), "Unsafe cleanup name")


def discard(work, names):
    # Delete named plaintext copies from a private job directory.
    work = job_dir(work)
    plain_names(names)
    for name in names:
        if (work / name).is_symlink() or (work / name).exists():
            remove_entry(work / name)


def retain(work, names):
    # After a failed import keep only the named diagnostic logs; everything else may hold secrets.
    work = job_dir(work)
    plain_names(names)
    for path in list(work.iterdir()):
        if path.name not in names:
            remove_entry(path)


def state_status(state):
    plain_path(state)
    require(state.is_dir(), "State directory is missing")
    require(not (state / ".import-in-progress").exists(),
            "Interrupted import: follow recovery instructions before retrying")
    return "occupied" if occupied(state) else "empty"


def handoff(state, action):
    # The marker tells the old host's systemd drop-in (pisobot-vps-handoff.conf) never to start the
    # watcher again; only a rollback import or a deliberate operator step removes it.
    plain_path(state)
    require(state.is_dir(), "State directory is missing")
    require(action in ("set", "clear"), "Unknown handoff action")
    marker = state / HANDOFF
    require(not marker.is_symlink(), "Handoff marker must not be a symlink")
    if action == "clear":
        marker.unlink(missing_ok=True)
    elif not marker.exists():
        with marker.open("x") as output:
            output.write(f"Final export/handoff started at {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}.\n"
                         "This fence does not prove that the export completed successfully.\n"
                         "Do not start pisobot-watch on this host; see cutover.md (Rollback).\n")


def utc(text):
    try:
        moment = dt.datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        return moment.replace(tzinfo=dt.timezone.utc)
    return moment.astimezone(dt.timezone.utc)


def last_success(snapshot_root):
    # pisobot.watch records each completed scan in the primary database's meta table.
    database = snapshot_root / "state/pisobot.db"
    with contextlib.closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
        if db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'").fetchone() is None:
            return None
        row = db.execute("SELECT value FROM meta WHERE name = 'watch_last_success'").fetchone()
    return utc(row[0]) if row else None


def replace_check(existing, restored, allow_older):
    # Refuse to move the live state back in time unless the operator says so explicitly.
    created = utc(json.loads((restored / "manifest.json").read_text())["created_utc"])
    require(created is not None, "Archive manifest has no valid creation time")
    last = last_success(existing)
    if last is None:
        print("WARNING: the existing state records no successful scan time, so it cannot be compared "
              "with the archive.", file=sys.stderr)
    elif last.replace(microsecond=0) > created:
        require(allow_older,
                f"Existing state is newer than the archive (last successful scan {last:%Y-%m-%d %H:%M}Z, "
                f"archive created {created:%Y-%m-%d %H:%M}Z); importing would lose those changes from the "
                "live state. Re-run with --allow-older-archive only if that is intended.")
        print("WARNING: replacing newer state with an older archive (--allow-older-archive).", file=sys.stderr)


def install_state(root, state, env, replace=False, expect=""):
    # Keep prior state until BOTH validation commands succeed. Re-imports replace,
    # never merge: a stale WAL or deleted user's files cannot survive the restore.
    # Replacing non-empty state needs the caller to have stored a verified durable copy
    # (expect = fingerprint of the state that copy was taken from).
    plain_path(state)
    plain_path(env)
    require(state.is_dir() and env.parent.is_dir(), "Provision state/config directories first")
    require(state not in root.parents and root not in state.parents, "Scratch and state must be separate trees")
    with stopped_locks(state, create=True):
        require(not (state / ".import-in-progress").exists(), "Interrupted import: follow recovery instructions before retrying")
        require(replace or not occupied(state), "Destination state is not empty; refusing to replace it")
        require(not expect or fingerprint(state) == expect,
                "Existing state changed after its encrypted copy was taken; nothing replaced")
        old_size = sum(path.stat().st_size for path in files(state))
        new_size = sum(path.stat().st_size for path in files(root / "state"))
        require(shutil.disk_usage(root).free > old_size + 16 * 1024**2, "Insufficient space to retain previous state")
        require(shutil.disk_usage(state).free > new_size + 16 * 1024**2, "Insufficient destination space")
        (state / ".import-in-progress").write_text("Import incomplete; do not enable the service.\n")
        old = root / "previous"
        old.mkdir(mode=0o700)
        for item in state.iterdir():
            if item.name in KEEP:
                continue
            require(not item.is_symlink(), "Existing state must not contain symlinks")
            shutil.move(str(item), str(old / item.name))
        if env.exists():
            shutil.copyfile(env, root / "previous.env")
        for item in (root / "state").iterdir():
            target = state / item.name
            if item.is_dir():
                shutil.copytree(item, target)
            else:
                shutil.copyfile(item, target)
        env_tmp = env.with_name(".pisobot.env.import")
        require(not env_tmp.exists() and not env_tmp.is_symlink(), "Stale env import file; inspect before retrying")
        with (root / "env/pisobot.env").open("rb") as source, env_tmp.open("xb") as dest:
            shutil.copyfileobj(source, dest)
        env_tmp.chmod(0o600)
        os.replace(env_tmp, env)


def permissions(state, env, uid, gid):
    for path in (state, *state.rglob("*")):
        require(not path.is_symlink(), "Refusing permissions on a symlink")
        path.chmod(0o700 if path.is_dir() else 0o600)
        os.chown(path, uid, gid)
    env.chmod(0o600)
    os.chown(env, os.geteuid(), os.getegid())


# Every variable the application reads (pisobot/config.py and the modules calling os.environ),
# except INSECURE_SSL, which disables TLS verification. A restored env file may set nothing else:
# PYTHONPATH, LD_PRELOAD and the like would change what code the service runs.
ALLOWED_ENV = frozenset("""
    CRIME_RADIUS_M DECODO_AUTH_TOKEN DEFAULT_SERVICE_CHARGE FETCH_CHAIN FETCH_MIN_GAP_S HEALTHCHECK_URL
    MAP_TILE_URL MAP_URL MAX_ALERTS_PER_RUN MAX_CRIME_SCORE MAX_DEPOSIT MAX_MONTHLY_COST MAX_TRAVEL_MINUTES
    MAX_USERS MIN_BEDROOMS MORTGAGE_RATE MORTGAGE_YEARS PISOBOT_STATE_DIR PREFERRED_DEPOSIT PRICE_DROP_ALERT
    RENT_BILLS_ESTIMATE RENT_COUNCIL_TAX_ESTIMATE RENT_MAX_PAGES RENT_MIN RENT_MIN_AREA_M2 RENT_REFRESH_HOURS
    RENT_REFRESH_MINUTES RENT_SEARCH_MILES REPORT_TOP_N SEARCH_RADIUS_KM SO_RENT_RATE STORAGE TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID TELEGRAM_INVITE_USERNAMES TFL_APP_KEY WATCH_FAILURE_LIMIT WATCH_INTERVAL_MIN WATCH_SOURCES
""".split())


def env_check(env, state):
    # Deliberately accept only the documented single-line EnvironmentFile syntax.
    # Never source credentials as shell code.
    import shlex

    values = {}
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, sep, raw = line.partition("=")
        require(sep and re.fullmatch(r"[A-Z_][A-Z_0-9]*", name), "Unsupported environment file syntax")
        # Only the validated name is ever printed, never a value.
        require(name in ALLOWED_ENV, f"Environment variable {name} is not one the application reads; "
                "remove it from the environment file and export again")
        parts = shlex.split(raw)
        require(len(parts) <= 1, "Unsupported environment file quoting")
        values[name] = parts[0] if parts else ""
    require(values.get("STORAGE") == "local", "Environment requires STORAGE=local")
    require(values.get("FETCH_CHAIN") == "plain", "Environment requires FETCH_CHAIN=plain")
    require(values.get("PISOBOT_STATE_DIR") == str(state), "Environment state path does not match destination")
    require(values.get("TELEGRAM_BOT_TOKEN") and values.get("TELEGRAM_CHAT_ID"), "Required Telegram configuration is missing")


def main(args):
    command, *args = args
    if command == "test-paths":
        test_paths(args[0], args[1:])
    elif command == "scratch-path":
        state, scratch = map(Path, args)
        plain_path(state)
        plain_path(scratch)
        require(state != scratch and state not in scratch.parents and scratch not in state.parents,
                "Scratch and state must be separate trees")
    elif command == "private-file":
        private_file(Path(args[0]))
    elif command == "snapshot":
        snapshot(*map(Path, args[:3]), *args[3:5], args[5] == "1", args[6] == "1")
    elif command == "pack":
        pack(Path(args[0]))
    elif command == "unpack":
        unpack(Path(args[0]))
    elif command == "verify":
        verify(Path(args[0]), *args[1:])
    elif command == "install":
        install_state(*map(Path, args[:3]), replace=args[3:4] == ["1"], expect=(args[4:5] or [""])[0])
    elif command == "state-status":
        print(state_status(Path(args[0])))
    elif command == "handoff":
        handoff(Path(args[0]), args[1])
    elif command == "fingerprint":
        print(fingerprint(Path(args[0])))
    elif command == "durable-path":
        durable_path(*map(Path, args))
    elif command == "persist":
        persist_files(*map(Path, args))
    elif command == "discard":
        discard(Path(args[0]), args[1:])
    elif command == "retain":
        retain(Path(args[0]), args[1:])
    elif command == "replace-check":
        replace_check(Path(args[0]), Path(args[1]), args[2] == "1")
    elif command == "permissions":
        permissions(*map(Path, args[:2]), *map(int, args[2:]))
    elif command == "env-check":
        env_check(*map(Path, args))
    elif command == "seen-check":
        old, new = map(Path, args)
        expected = {p.relative_to(old): digest(p) for p in old.rglob("seen.json")}
        actual = {p.relative_to(new): digest(p) for p in new.rglob("seen.json")}
        require(expected == actual, "Dry-run unexpectedly changed seen state")
    elif command == "checksum-write":
        path = Path(args[0])
        checksum = digest(path)
        with Path(str(path) + ".sha256").open("x") as output:
            output.write(checksum + "\n")
        print(f"SHA256 {checksum}  {path.name}")
    elif command == "checksum-check":
        expected = Path(args[1]).read_text().strip()
        require(re.fullmatch(r"[a-f0-9]{64}", expected), "Expected a digest-only SHA256 file")
        require(digest(Path(args[0])) == expected, "Encrypted archive SHA256 mismatch")
        print("Encrypted archive SHA256 OK")
    elif command == "cleanup":
        parent, child = map(Path, args)
        plain_path(parent)
        plain_path(child)
        require(child.parent == parent and child.name.startswith("job."), "Unsafe cleanup target")
        shutil.rmtree(child)
    elif command == "healthcheck":
        url = os.environ.get("HEALTHCHECK_URL", "")
        if url:
            require(url.startswith("https://"), "Healthcheck requires HTTPS")
            with urllib.request.urlopen(url, timeout=15) as response:
                require(200 <= response.status < 300, "Healthcheck failed")
            print("Backup healthcheck OK")
    else:
        raise ValueError("Unknown helper command")


if __name__ == "__main__":
    os.umask(0o077)
    try:
        main(sys.argv[1:])
    except (OSError, ValueError, KeyError, sqlite3.Error, tarfile.TarError) as error:
        # Exception messages from parsers/network libraries can contain secrets.
        if isinstance(error, ValueError) and type(error) is ValueError:
            print(f"ERROR: {error}", file=sys.stderr)
        else:
            print(f"ERROR: migration helper failed ({type(error).__name__}); no secret values logged.", file=sys.stderr)
        sys.exit(1)
