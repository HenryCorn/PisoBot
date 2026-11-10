"""Private implementation helpers for the guarded shell commands; stdlib only."""

import contextlib
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


def ignored(path):
    return path.name.endswith((".lock", ".pending", "-wal", "-shm", "-journal"))


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
def stopped_locks(root):
    # These are the same locks used by pisobot.main.run_lock, not PID files.
    with contextlib.ExitStack() as stack:
        for name in ("watch.lock", "run.lock"):
            path = root / name
            require(not path.is_symlink(), "Lock must not be a symlink")
            stream = stack.enter_context(path.open("a"))
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def snapshot(state, env, dest, revision, host, quiesced, final):
    plain_path(state)
    plain_path(env)
    require(state.is_dir(), "State directory is missing")
    private_file(env)
    require(re.fullmatch(r"[a-f0-9]{40,64}", revision), "Cannot identify application commit")
    dest.mkdir(mode=0o700)
    require(state not in dest.parents, "Scratch must be outside the state directory")
    lock = stopped_locks(state) if quiesced else contextlib.nullcontext()
    with lock:
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

                def progress(_status, _remaining, _total):
                    require(time.monotonic() < deadline, "SQLite online backup timed out")

                with contextlib.closing(sqlite3.connect(src.as_uri() + "?mode=ro", uri=True)) as source:
                    with contextlib.closing(sqlite3.connect(target)) as backup:
                        source.backup(backup, pages=256, progress=progress, sleep=0.1)
                        backup.execute("PRAGMA journal_mode=DELETE")
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


def install_state(root, state, env):
    # Keep prior state until BOTH validation commands succeed. Re-imports replace,
    # never merge: a stale WAL or deleted user's files cannot survive the restore.
    plain_path(state)
    plain_path(env)
    require(state.is_dir() and env.parent.is_dir(), "Provision state/config directories first")
    require(state not in root.parents and root not in state.parents, "Scratch and state must be separate trees")
    with stopped_locks(state):
        require(not (state / ".import-in-progress").exists(), "Interrupted import: follow recovery instructions before retrying")
        old_size = sum(path.stat().st_size for path in files(state))
        new_size = sum(path.stat().st_size for path in files(root / "state"))
        require(shutil.disk_usage(root).free > old_size + 16 * 1024**2, "Insufficient space to retain previous state")
        require(shutil.disk_usage(state).free > new_size + 16 * 1024**2, "Insufficient destination space")
        (state / ".import-in-progress").write_text("Import incomplete; do not enable the service.\n")
        old = root / "previous"
        old.mkdir(mode=0o700)
        for item in state.iterdir():
            if item.name in ("watch.lock", "run.lock", ".import-in-progress"):
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
        install_state(*map(Path, args))
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
