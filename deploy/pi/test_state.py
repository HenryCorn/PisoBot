"""Offline unit tests for state.py: no age, no root, no systemd, synthetic data only.

    cd deploy/pi && python3 -m unittest test_state -v
"""

import contextlib
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import state as kit  # noqa: E402

REVISION = "a" * 40


def make_db(path, rows=1, last_success=None):
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE items(x)")
    db.executemany("INSERT INTO items VALUES (?)", [(i,) for i in range(rows)])
    if last_success is not None:
        db.execute("CREATE TABLE meta(name TEXT PRIMARY KEY, value TEXT)")
        db.execute("INSERT INTO meta VALUES ('watch_last_success', ?)", (last_success,))
    db.commit()
    db.close()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pisobot-unit-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.state = self.tmp / "state"
        self.state.mkdir(mode=0o700)
        self.env = self.tmp / "pisobot.env"
        self.env.write_text("TELEGRAM_BOT_TOKEN=synthetic\n")
        self.env.chmod(0o600)
        self.snapshots = 0

    def fill(self, locks=True):
        make_db(self.state / "pisobot.db")
        (self.state / "seen.json").write_text("{}")
        (self.state / "cache.json").write_text("{}")
        if locks:
            (self.state / "watch.lock").touch()
            (self.state / "run.lock").touch()

    def snapshot(self, quiesced, final=False):
        self.snapshots += 1
        dest = self.tmp / f"snapshot-{self.snapshots}"
        with contextlib.redirect_stderr(io.StringIO()):
            kit.snapshot(self.state, self.env, dest, REVISION, "synthetic-host", quiesced, final)
        return dest

    def names(self, directory):
        return sorted(p.name for p in directory.iterdir())


class IgnoredTests(unittest.TestCase):
    def test_runtime_only_files_are_never_snapshotted(self):
        names = ("watch.lock", "run.lock", "pisobot.db-wal", "pisobot.db-shm", "pisobot.db-journal",
                 "cache.json.pending", kit.HANDOFF, "tmpa1b2c3d4", "tmp_x_y_z_1")
        for name in names:
            self.assertTrue(kit.ignored(Path("/state") / name), name)

    def test_real_state_is_never_ignored(self):
        names = ("pisobot.db", "seen.json", "cache.json", "saved.json", "tmp12345", "tmpABCDEFGH",
                 "tmp123456789", "tmp-abcdefg", "mytmpabcdefgh", "notes.tmp")
        for name in names:
            self.assertFalse(kit.ignored(Path("/state/users/1") / name), name)


class StatusTests(Base):
    def test_only_runtime_entries_leave_the_state_empty(self):
        self.assertEqual("empty", kit.state_status(self.state))
        for name in ("watch.lock", "run.lock", kit.HANDOFF):
            (self.state / name).touch()
        (self.state / "lost+found").mkdir()
        self.assertEqual("empty", kit.state_status(self.state))
        self.assertFalse(kit.occupied(self.state))

    def test_any_data_makes_the_state_occupied(self):
        (self.state / "users").mkdir()
        self.assertEqual("occupied", kit.state_status(self.state))

    def test_interrupted_import_is_refused(self):
        (self.state / ".import-in-progress").touch()
        with self.assertRaisesRegex(ValueError, "Interrupted import"):
            kit.state_status(self.state)


class FingerprintTests(Base):
    def test_tracks_data_but_not_runtime_files(self):
        self.fill()
        before = kit.fingerprint(self.state)
        for name in ("watch.lock", kit.HANDOFF, "pisobot.db-shm", "tmpa1b2c3d4"):
            (self.state / name).write_text("noise")
        self.assertEqual(before, kit.fingerprint(self.state))
        (self.state / "seen.json").write_text('{"a": 1}')
        self.assertNotEqual(before, kit.fingerprint(self.state))

    def test_commits_only_in_the_wal_change_the_fingerprint(self):
        self.fill()
        with contextlib.closing(sqlite3.connect(self.state / "pisobot.db")) as db:
            db.execute("PRAGMA wal_autocheckpoint=0")
            db.execute("INSERT INTO items VALUES (10)")
            db.commit()
            main_before = kit.digest(self.state / "pisobot.db")
            before = kit.fingerprint(self.state)
            db.execute("INSERT INTO items VALUES (20)")
            db.commit()
            self.assertEqual(main_before, kit.digest(self.state / "pisobot.db"))
            self.assertNotEqual(before, kit.fingerprint(self.state))

    def test_a_touch_alone_changes_it(self):
        self.fill()
        before = kit.fingerprint(self.state)
        seen = self.state / "seen.json"
        os.utime(seen, ns=(seen.stat().st_atime_ns, seen.stat().st_mtime_ns + 1_000_000_000))
        self.assertNotEqual(before, kit.fingerprint(self.state))


class JobDirectoryTests(Base):
    def job(self):
        job = self.tmp / "job.abc"
        job.mkdir(mode=0o700)
        (job / "state").mkdir()
        (job / "state/seen.json").write_text("secret")
        (job / "export.age").write_text("x")
        (job / "unit-tests.log").write_text("log")
        (job / "dry-run.log").write_text("log")
        outside = self.tmp / "elsewhere"
        outside.mkdir()
        (outside / "keep.txt").write_text("keep")
        (job / "link").symlink_to(outside)
        return job

    def test_retain_keeps_only_the_named_logs_and_never_follows_links(self):
        job = self.job()
        kit.retain(job, ("unit-tests.log", "dry-run.log"))
        self.assertEqual(["dry-run.log", "unit-tests.log"], self.names(job))
        self.assertEqual("keep", (self.tmp / "elsewhere/keep.txt").read_text())

    def test_discard_removes_only_the_named_entries(self):
        job = self.job()
        kit.discard(job, ("state", "export.age", "link", "never-existed"))
        self.assertEqual(["dry-run.log", "unit-tests.log"], self.names(job))
        self.assertEqual("keep", (self.tmp / "elsewhere/keep.txt").read_text())

    def test_unsafe_targets_and_names_are_refused(self):
        job = self.job()
        other = self.tmp / "other"
        other.mkdir()
        with self.assertRaisesRegex(ValueError, "Unsafe cleanup target"):
            kit.retain(other, ())
        for name in ("../state", "a/b", "..", ""):
            with self.assertRaisesRegex(ValueError, "Unsafe cleanup name"):
                kit.discard(job, (name,))

    def test_encrypted_copy_directory_must_be_separate_from_state_and_scratch(self):
        durable = self.tmp / "backups"
        durable.mkdir()
        kit.durable_path(durable, self.state, self.tmp / "job.x")
        for directory in (self.state / "inside", self.tmp, self.state):
            with self.assertRaisesRegex(ValueError, "separate"):
                kit.durable_path(directory, self.state)


class ReplaceCheckTests(Base):
    def snapshot_root(self, name, last_success=None, created=None):
        root = self.tmp / name
        (root / "state").mkdir(parents=True)
        make_db(root / "state/pisobot.db", last_success=last_success)
        if created:
            (root / "manifest.json").write_text(json.dumps({"created_utc": created}))
        return root

    def check(self, existing_last, created, allow):
        self.snapshots += 1
        existing = self.snapshot_root(f"existing-{self.snapshots}", existing_last)
        restored = self.snapshot_root(f"restored-{self.snapshots}", created=created)
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            kit.replace_check(existing, restored, allow)
        return errors.getvalue()

    def test_newer_existing_state_is_refused_unless_explicitly_allowed(self):
        existing, archive = "2025-06-02T10:00:00+00:00", "2025-06-01T10:00:00Z"
        with self.assertRaisesRegex(ValueError, "newer than the archive.*--allow-older-archive"):
            self.check(existing, archive, False)
        self.assertIn("WARNING", self.check(existing, archive, True))

    def test_older_or_equal_existing_state_passes_quietly(self):
        self.assertEqual("", self.check("2025-06-01T09:00:00+00:00", "2025-06-01T10:00:00Z", False))

    def test_sub_second_precision_does_not_cause_a_false_refusal(self):
        self.assertEqual("", self.check("2025-06-01T10:00:00.700000+00:00", "2025-06-01T10:00:00Z", False))

    def test_unknown_scan_time_warns_but_does_not_block(self):
        self.assertIn("cannot be compared", self.check(None, "2025-06-01T10:00:00Z", False))

    def test_invalid_archive_time_is_refused(self):
        with self.assertRaisesRegex(ValueError, "no valid creation time"):
            self.check("2025-06-01T09:00:00+00:00", "not-a-time", False)


class PersistenceTests(Base):
    def test_ciphertext_checksum_and_new_directory_ancestry_are_synced_leaf_first(self):
        directory = self.tmp / "new/backups"
        directory.mkdir(parents=True)
        copy = directory / "copy.age"
        checksum = directory / "copy.age.sha256"
        copy.write_bytes(b"synthetic ciphertext")
        checksum.write_text(kit.digest(copy))
        with patch.object(kit, "sync_path", wraps=kit.sync_path) as sync:
            kit.persist_files(copy, checksum)
        self.assertEqual([call.args[0] for call in sync.call_args_list],
                         [copy, checksum, directory, *directory.parents])
        self.assertEqual([call.kwargs for call in sync.call_args_list],
                         [{}, {}, *({"directory": True} for _ in (directory, *directory.parents))])


class InstallTests(Base):
    def setUp(self):
        super().setUp()
        self.root = self.tmp / "job.1"
        (self.root / "state").mkdir(parents=True, mode=0o700)
        (self.root / "env").mkdir(mode=0o700)
        (self.root / "state/pisobot.db").write_text("restored-db")
        (self.root / "state/seen.json").write_text('{"restored": true}')
        (self.root / "state/cache.json").write_text("{}")
        (self.root / "env/pisobot.env").write_text("TELEGRAM_BOT_TOKEN=restored\n")
        config = self.tmp / "etc"
        config.mkdir(mode=0o700)
        self.target_env = config / "pisobot.env"

    def install(self, **options):
        kit.install_state(self.root, self.state, self.target_env, **options)

    def test_fresh_destination_receives_the_state_and_private_locks(self):
        self.install()
        self.assertEqual('{"restored": true}', (self.state / "seen.json").read_text())
        for name in ("watch.lock", "run.lock"):
            self.assertEqual(0o600, (self.state / name).stat().st_mode & 0o777)
        self.assertTrue((self.state / ".import-in-progress").exists())
        self.assertEqual("TELEGRAM_BOT_TOKEN=restored\n", self.target_env.read_text())

    def test_occupied_destination_is_refused_without_replace(self):
        self.fill()
        with self.assertRaisesRegex(ValueError, "not empty"):
            self.install()
        self.assertFalse((self.state / ".import-in-progress").exists())
        self.assertEqual("{}", (self.state / "seen.json").read_text())

    def test_replace_moves_the_old_state_aside_and_keeps_runtime_entries(self):
        self.fill()
        (self.state / kit.HANDOFF).write_text("marker")
        (self.state / "lost+found").mkdir()
        self.install(replace=True, expect=kit.fingerprint(self.state))
        self.assertTrue((self.root / "previous/pisobot.db").is_file())
        self.assertEqual('{"restored": true}', (self.state / "seen.json").read_text())
        for name in (kit.HANDOFF, "lost+found", "watch.lock", "run.lock"):
            self.assertTrue((self.state / name).exists(), name)

    def test_replace_is_refused_when_the_state_changed_after_its_encrypted_copy(self):
        self.fill()
        taken = kit.fingerprint(self.state)
        (self.state / "seen.json").write_text('{"written": "later"}')
        with self.assertRaisesRegex(ValueError, "changed after its encrypted copy"):
            self.install(replace=True, expect=taken)
        self.assertFalse((self.state / ".import-in-progress").exists())
        self.assertEqual('{"written": "later"}', (self.state / "seen.json").read_text())

    def test_interrupted_import_is_refused(self):
        (self.state / ".import-in-progress").touch()
        with self.assertRaisesRegex(ValueError, "Interrupted import"):
            self.install()


class LockTests(Base):
    def test_missing_lock_is_refused_and_never_created(self):
        with self.assertRaisesRegex(ValueError, "watch.lock is missing"):
            with kit.stopped_locks(self.state):
                pass
        (self.state / "watch.lock").touch()
        with self.assertRaisesRegex(ValueError, "run.lock is missing"):
            with kit.stopped_locks(self.state):
                pass
        self.assertEqual(["watch.lock"], self.names(self.state))

    def test_only_an_import_may_create_the_locks(self):
        with kit.stopped_locks(self.state, create=True):
            pass
        for name in ("watch.lock", "run.lock"):
            self.assertEqual(0o600, (self.state / name).stat().st_mode & 0o777)

    def test_a_held_lock_blocks_a_second_holder(self):
        self.fill()
        with kit.stopped_locks(self.state):
            with self.assertRaises(BlockingIOError):
                with kit.stopped_locks(self.state):
                    pass

    def test_symlinked_lock_is_refused(self):
        (self.state / "watch.lock").symlink_to(self.tmp / "elsewhere")
        with self.assertRaisesRegex(ValueError, "symlink"):
            with kit.stopped_locks(self.state, create=True):
                pass
        self.assertFalse((self.tmp / "elsewhere").exists())


class SnapshotTests(Base):
    def test_final_fence_is_persisted_while_both_application_locks_are_held(self):
        self.fill()
        persist = kit.persist_files

        def guarded(*paths):
            self.assertEqual(paths, (self.state / kit.HANDOFF,))
            for name in ("watch.lock", "run.lock"):
                with (self.state / name).open("rb") as stream:
                    with self.assertRaises(BlockingIOError):
                        kit.fcntl.flock(stream, kit.fcntl.LOCK_EX | kit.fcntl.LOCK_NB)
            persist(*paths)

        with patch.object(kit, "persist_files", side_effect=guarded) as sync:
            dest = self.snapshot(quiesced=True, final=True)
        sync.assert_called_once()
        self.assertTrue((self.state / kit.HANDOFF).is_file())
        self.assertNotIn(kit.HANDOFF, self.names(dest / "state"))

    def test_failed_final_fence_sync_keeps_marker_and_stops_before_snapshot(self):
        self.fill()
        before = kit.fingerprint(self.state)
        with patch.object(kit.os, "fsync", side_effect=OSError("synthetic sync failure")):
            with self.assertRaises(OSError):
                self.snapshot(quiesced=True, final=True)
        self.assertTrue((self.state / kit.HANDOFF).is_file())
        self.assertEqual(before, kit.fingerprint(self.state))
        self.assertFalse((self.tmp / "snapshot-1/state").exists())

    def test_invalid_final_snapshot_leaves_its_fail_closed_fence(self):
        self.fill()
        (self.state / "seen.json").write_text("{invalid")
        with self.assertRaises(ValueError):
            self.snapshot(quiesced=True, final=True)
        self.assertTrue((self.state / kit.HANDOFF).is_file())

    def test_final_without_application_locks_is_refused(self):
        self.fill()
        with self.assertRaisesRegex(ValueError, "requires the application locks"):
            self.snapshot(quiesced=False, final=True)
        self.assertFalse((self.state / kit.HANDOFF).exists())

    def test_quiesced_snapshot_removes_only_the_sidecars_it_caused(self):
        self.fill()
        before = self.names(self.state)
        dest = self.snapshot(quiesced=True)
        self.assertEqual(before, self.names(self.state))
        self.assertEqual(["cache.json", "pisobot.db", "seen.json"], self.names(dest / "state"))

    def test_quiesced_snapshot_keeps_sidecars_that_were_already_there(self):
        self.fill()
        (self.state / "pisobot.db-wal").write_bytes(b"")
        self.snapshot(quiesced=True)
        self.assertTrue((self.state / "pisobot.db-wal").exists())

    def test_live_snapshot_is_wal_consistent_and_leaves_the_services_files_alone(self):
        self.fill()
        writer = sqlite3.connect(self.state / "pisobot.db")
        self.addCleanup(writer.close)
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("INSERT INTO items VALUES (4242)")
        writer.commit()
        self.assertGreater((self.state / "pisobot.db-wal").stat().st_size, 0)
        dest = self.snapshot(quiesced=False)
        with contextlib.closing(sqlite3.connect(dest / "state/pisobot.db")) as copy:
            self.assertIn((4242,), copy.execute("SELECT x FROM items").fetchall())
        self.assertTrue((self.state / "pisobot.db-wal").exists())
        self.assertTrue((self.state / "pisobot.db-shm").exists())
        self.assertFalse([p for p in dest.rglob("*") if p.name.endswith(("-wal", "-shm"))])

    def test_quiesced_snapshot_without_locks_is_refused_and_creates_none(self):
        self.fill(locks=False)
        with self.assertRaisesRegex(ValueError, "watch.lock is missing"):
            self.snapshot(quiesced=True)
        self.assertFalse((self.state / "watch.lock").exists())
        self.assertFalse((self.state / "run.lock").exists())

    def test_handoff_marker_and_half_written_seen_files_stay_out_of_the_snapshot(self):
        self.fill()
        kit.handoff(self.state, "set")
        (self.state / "tmpa1b2c3d4").write_text("{half")
        dest = self.snapshot(quiesced=False)
        self.assertEqual(["cache.json", "pisobot.db", "seen.json"], self.names(dest / "state"))


class HandoffTests(Base):
    def test_set_writes_a_marker_and_clear_removes_it(self):
        kit.handoff(self.state, "set")
        self.assertIn("Do not start pisobot-watch", (self.state / kit.HANDOFF).read_text())
        kit.handoff(self.state, "set")
        kit.handoff(self.state, "clear")
        kit.handoff(self.state, "clear")
        self.assertFalse((self.state / kit.HANDOFF).exists())

    def test_symlinked_marker_and_unknown_actions_are_refused(self):
        (self.state / kit.HANDOFF).symlink_to(self.tmp / "elsewhere")
        with self.assertRaisesRegex(ValueError, "symlink"):
            kit.handoff(self.state, "set")
        (self.state / kit.HANDOFF).unlink()
        with self.assertRaisesRegex(ValueError, "Unknown handoff action"):
            kit.handoff(self.state, "toggle")


class EnvTests(Base):
    def lines(self, *extra):
        return ["STORAGE=local", "FETCH_CHAIN=plain", f"PISOBOT_STATE_DIR={self.state}",
                "TELEGRAM_BOT_TOKEN=synthetic-token", "TELEGRAM_CHAT_ID='123'", *extra]

    def write(self, lines):
        path = self.tmp / "candidate.env"
        path.write_text("\n".join(lines) + "\n")
        return path

    def test_a_valid_file_passes(self):
        kit.env_check(self.write(["# comment", "", *self.lines("MAX_USERS=2", "WATCH_INTERVAL_MIN=30")]), self.state)

    def test_names_the_application_does_not_read_are_refused_without_echoing_values(self):
        for name in ("PYTHONPATH", "LD_PRELOAD", "PATH", "INSECURE_SSL", "SOMETHING_NEW"):
            with self.subTest(name):
                with self.assertRaises(ValueError) as caught:
                    kit.env_check(self.write(self.lines(f"{name}=/tmp/secret-value-123")), self.state)
                self.assertIn(name, str(caught.exception))
                self.assertNotIn("secret-value-123", str(caught.exception))

    def test_shell_syntax_is_refused(self):
        for line in ("export MAX_USERS=2", "max_users=2", "MAX_USERS=$(id)x y", "MAX_USERS"):
            with self.subTest(line):
                with self.assertRaises(ValueError):
                    kit.env_check(self.write(self.lines(line)), self.state)

    def test_allowlist_matches_what_the_application_and_example_use(self):
        root = HERE.parents[1]
        used = set()
        for source in (root / "pisobot").rglob("*.py"):
            text = source.read_text()
            used |= set(re.findall(r"""(?:_env|environ\.get|getenv)\(\s*["']([A-Z][A-Z_0-9]*)["']""", text))
            used |= set(re.findall(r"""environ\[\s*["']([A-Z][A-Z_0-9]*)["']\s*\]""", text))
        example = root / "deploy/vps/pisobot.env.example"
        used |= set(re.findall(r"^#?\s*([A-Z][A-Z_0-9]*)=", example.read_text(), re.M))
        # INSECURE_SSL switches off certificate checks, so a restored file may never set it.
        self.assertEqual(set(), used - kit.ALLOWED_ENV - {"INSECURE_SSL"})
        self.assertIn("TELEGRAM_BOT_TOKEN", used)


class ServiceGuardTests(Base):
    def test_fenced_guard_accepts_only_stopped_disabled_or_masked_units(self):
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        stub = bin_dir / "systemctl"
        stub.write_text(
            "#!/bin/bash\n"
            "case \"$4\" in\n"
            "LoadState) echo \"$FAKE_LOAD\" ;;\n"
            "ActiveState) echo \"$FAKE_ACTIVE\" ;;\n"
            "UnitFileState) echo \"$FAKE_UNIT\" ;;\n"
            "*) exit 99 ;;\nesac\n"
        )
        stub.chmod(0o700)
        cases = (
            ("loaded", "inactive", "disabled", "fenced", True),
            ("masked", "inactive", "masked", "fenced", True),
            ("loaded", "failed", "disabled", "fenced", True),
            ("loaded", "active", "disabled", "fenced", False),
            ("loaded", "inactive", "enabled", "fenced", False),
            ("loaded", "inactive", "static", "fenced", False),
            ("not-found", "inactive", "disabled", "fenced", False),
            ("masked", "inactive", "masked", "disabled", False),
        )
        for loaded, active, unit, mode, accepted in cases:
            with self.subTest(loaded=loaded, active=active, unit=unit, mode=mode):
                result = subprocess.run(
                    ["/bin/bash", "-c", 'source "$1"; guard_stopped "$2"', "guard-test",
                     str(HERE / "common.bash"), mode],
                    env=dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", PISOBOT_TEST_ROOT="",
                             FAKE_LOAD=loaded, FAKE_ACTIVE=active, FAKE_UNIT=unit),
                    capture_output=True, text=True, timeout=10,
                )
                self.assertEqual(accepted, result.returncode == 0, result.stderr)


class CommandLineTests(Base):
    def helper(self, *args):
        return subprocess.run([sys.executable, str(HERE / "state.py"), *map(str, args)], capture_output=True,
                              text=True, timeout=60, env={"PATH": os.environ.get("PATH", ""),
                                                          "PYTHONDONTWRITEBYTECODE": "1"})

    def test_refusals_print_a_message_and_never_the_value(self):
        candidate = self.tmp / "candidate.env"
        candidate.write_text("STORAGE=local\nPYTHONPATH=/tmp/secret-value-123\n")
        result = self.helper("env-check", candidate, self.state)
        self.assertEqual(1, result.returncode)
        self.assertIn("ERROR: Environment variable PYTHONPATH is not one the application reads", result.stderr)
        self.assertNotIn("secret-value-123", result.stdout + result.stderr)

    def test_handoff_and_status_commands(self):
        self.assertEqual("empty\n", self.helper("state-status", self.state).stdout)
        self.assertEqual(0, self.helper("handoff", self.state, "set").returncode)
        self.assertTrue((self.state / kit.HANDOFF).is_file())
        self.assertEqual("empty\n", self.helper("state-status", self.state).stdout)
        self.assertEqual(0, self.helper("handoff", self.state, "clear").returncode)
        self.assertFalse((self.state / kit.HANDOFF).exists())
        (self.state / "seen.json").write_text("{}")
        self.assertEqual("occupied\n", self.helper("state-status", self.state).stdout)


if __name__ == "__main__":
    unittest.main()
