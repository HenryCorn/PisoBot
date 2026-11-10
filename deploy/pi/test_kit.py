"""Offline end-to-end kit tests: real age/SQLite, fake app, no hosts or services."""

import contextlib
import fcntl
import hashlib
import io
import os
from pathlib import Path
import platform
import shutil
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest


KIT = Path(__file__).resolve().parent


class KitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("age") or not shutil.which("age-keygen"):
            raise RuntimeError("Missing dependency: age and age-keygen (install age, then re-run)")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="pisobot-kit-test-", dir=KIT)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.root.chmod(0o700)
        (self.root / ".fake-state-only").write_text("fake-state-only\n")
        self.source = self.root / "source"
        self.target = self.root / "target"
        self.app = self.root / "app"
        for directory in (self.source, self.target, self.app, self.root / "config", self.root / "scratch",
                          self.root / "venv/bin", self.source / "users/123", self.source / "tiles"):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        (self.root / "venv/bin/python").symlink_to(sys.executable)
        for name in ("watch.lock", "run.lock"):
            (self.source / name).touch()
        self.env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
                        PISOBOT_TEST_ROOT=str(self.root),
                        PISOBOT_EXPECT_HOST=subprocess.check_output(["hostname", "-s"], text=True).strip(),
                        PISOBOT_EXPECT_ARCH=platform.machine(),
                        PISOBOT_STATE_DIR=str(self.source),
                        PISOBOT_ENV_FILE=str(self.root / "source.env"),
                        PISOBOT_APP_DIR=str(self.app),
                        PISOBOT_VENV_DIR=str(self.root / "venv"),
                        PISOBOT_TMP_DIR=str(self.root / "scratch"),
                        PISOBOT_PREVIOUS_DIR=str(self.root / "previous"),
                        PISOBOT_AGE_RECIPIENTS=str(self.root / "recipient"),
                        PISOBOT_AGE_IDENTITY=str(self.root / "identity"))
        subprocess.run(["age-keygen", "-o", str(self.root / "identity")], check=True, capture_output=True)
        (self.root / "identity").chmod(0o600)
        recipient = subprocess.check_output(["age-keygen", "-y", str(self.root / "identity")])
        (self.root / "recipient").write_bytes(recipient)
        for db_path in (self.source / "pisobot.db", self.source / "users/123/pisobot.db"):
            db = sqlite3.connect(db_path)
            self.addCleanup(db.close)
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA wal_autocheckpoint=0")
            db.executescript("CREATE TABLE choices (key TEXT, choice TEXT); "
                             "INSERT INTO choices VALUES ('rm:1', 'saved'), ('rm:2', 'hidden');")
            db.commit()
            self.assertGreater(Path(str(db_path) + "-wal").stat().st_size, 0)
            (db_path.parent / "seen.json").write_text('{"rm:1": {"alerted": true, "cost": 1200}}\n')
        (self.source / "cache.json").write_text('{"meta": {"watch_initialized": "1"}}\n')
        (self.source / "tiles/test.png").write_bytes(b"fake tile bytes")
        (self.source / ".cache.pending").write_text("partial")
        (self.root / "source.env").write_text(
            "TELEGRAM_BOT_TOKEN=FAKE-NOT-A-REAL-SECRET\nTELEGRAM_CHAT_ID=123\n"
            f"STORAGE=local\nFETCH_CHAIN=plain\nPISOBOT_STATE_DIR={self.target}\n"
        )
        (self.root / "source.env").chmod(0o600)
        self.archive = self.root / "snapshot.age"
        (self.app / "tests").mkdir()
        (self.app / "pisobot").mkdir()
        (self.app / "pisobot/__init__.py").write_text("")
        (self.app / "tests/test_fixture.py").write_text(
            "import os, unittest\nfrom pathlib import Path\n"
            "class Fixture(unittest.TestCase):\n"
            "    def test_isolated(self):\n"
            "        self.assertNotIn('TELEGRAM_BOT_TOKEN', os.environ)\n"
            "        self.assertNotIn('PISOBOT_STATE_DIR', os.environ)\n"
            "        root = Path(__file__).resolve().parents[1]\n"
            "        (root / 'unit-tests-ran').touch()\n"
            "        self.assertFalse((root / 'fail-unit').exists())\n"
        )
        (self.app / "pisobot/watch.py").write_text(
            "import os, sys\nfrom pathlib import Path\n"
            "assert sys.argv[1:] == ['--once', '--dry-run']\n"
            "root = Path(__file__).resolve().parents[1]\n"
            "assert not (root / 'fail-scan').exists()\n"
            "(Path(os.environ['PISOBOT_STATE_DIR']) / 'dry-run-ran').touch()\n"
        )
        for command in (["git", "init", "--quiet", str(self.app)],
                        ["git", "-C", str(self.app), "add", "."],
                        ["git", "-C", str(self.app), "-c", "user.name=Kit test", "-c", "user.email=test@example.invalid",
                         "commit", "--quiet", "-m", "Fixture\n\nCo-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"]):
            subprocess.run(command, check=True, capture_output=True)

    def invoke(self, script, *args, importing=False, success=True, extra_env=None):
        env = self.env.copy()
        if importing:
            env.update(PISOBOT_STATE_DIR=str(self.target), PISOBOT_ENV_FILE=str(self.root / "config/pisobot.env"))
        env.update(extra_env or {})
        result = subprocess.run(["bash", str(KIT / script), *args], env=env, text=True, capture_output=True)
        output = result.stdout + result.stderr
        self.assertNotIn("FAKE-NOT-A-REAL-SECRET", output)
        if success:
            self.assertEqual(result.returncode, 0, output)
        else:
            self.assertNotEqual(result.returncode, 0, output)
        return output

    def export(self, *args):
        return self.invoke("vps-export.sh", "--archive", str(self.archive), *args)

    def import_archive(self, *args, success=True):
        return self.invoke("pi-import.sh", "--archive", str(self.archive), *args, importing=True, success=success)

    @property
    def acknowledge(self):
        return ("--source-stopped", self.env["PISOBOT_EXPECT_HOST"])

    def replaced_copies(self):
        return sorted((self.root / "previous").glob("replaced-*.age"))

    def copy_members(self, copy):
        # Decrypt a stored copy of replaced state and return {archive member name: bytes}.
        result = subprocess.run(["age", "--decrypt", "-i", str(self.root / "identity"), str(copy)],
                                capture_output=True, check=True)
        with tarfile.open(fileobj=io.BytesIO(result.stdout), mode="r:gz") as archive:
            return {member.name: archive.extractfile(member).read() for member in archive.getmembers()}

    def restore_test(self, archive):
        return self.invoke("pi-import.sh", "--archive", str(archive), "--restore-test", importing=True)

    def test_live_wal_roundtrip_permissions_and_repeat_import(self):
        output = self.export("--final")
        self.assertIn("2 SQLite databases", output)
        self.assertNotIn(b"FAKE-NOT-A-REAL-SECRET", self.archive.read_bytes())
        self.assertIn("IMPORT OK", self.import_archive(*self.acknowledge))
        for relative in ("pisobot.db", "users/123/pisobot.db"):
            with contextlib.closing(sqlite3.connect(self.target / relative)) as db:
                self.assertEqual(db.execute("SELECT choice FROM choices ORDER BY key").fetchall(),
                                 [("saved",), ("hidden",)])
            self.assertFalse(Path(str(self.target / relative) + "-wal").exists())
        for relative in ("seen.json", "users/123/seen.json", "cache.json", "tiles/test.png"):
            self.assertEqual((self.source / relative).read_bytes(), (self.target / relative).read_bytes())
        self.assertFalse((self.target / ".cache.pending").exists())
        self.assertTrue((self.app / "unit-tests-ran").is_file())
        self.assertTrue((self.target / "dry-run-ran").is_file())
        self.assertEqual((self.root / "source.env").read_bytes(), (self.root / "config/pisobot.env").read_bytes())
        for path in (self.target, *self.target.rglob("*"), self.root / "config/pisobot.env"):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700 if path.is_dir() else 0o600)
            self.assertEqual(path.stat().st_uid, os.getuid())
        (self.target / "stale").write_text("must not survive")
        self.assertIn("already holds data", self.import_archive(*self.acknowledge, success=False))
        self.assertTrue((self.target / "stale").exists())
        self.import_archive(*self.acknowledge, "--replace-state")
        self.assertFalse((self.target / "stale").exists())
        self.assertEqual(list((self.root / "scratch").iterdir()), [])
        (copy,) = self.replaced_copies()
        self.assertEqual(self.copy_members(copy)["state/stale"], b"must not survive")

    def test_occupied_destination_needs_replace_state_and_leaves_a_restorable_encrypted_copy(self):
        self.export("--final")
        self.import_archive(*self.acknowledge)
        (self.target / "newer-note").write_text("written after the first import")
        refusals = (
            ((), {}, "already holds data"),
            (("--replace-state",), {"PISOBOT_AGE_RECIPIENTS": ""}, "PISOBOT_AGE_RECIPIENTS"),
            (("--replace-state",), {"PISOBOT_PREVIOUS_DIR": str(self.root / "scratch/previous")}, "must be separate"),
            (("--replace-state",), {"PISOBOT_PREVIOUS_DIR": str(self.target / "previous")}, "must be separate"),
        )
        for flags, extra, message in refusals:
            with self.subTest(flags=flags, extra=extra):
                output = self.invoke("pi-import.sh", "--archive", str(self.archive), *self.acknowledge, *flags,
                                     importing=True, success=False, extra_env=extra)
                self.assertIn(message, output)
                self.assertTrue((self.target / "newer-note").is_file())
                self.assertFalse((self.target / ".import-in-progress").exists())
                self.assertFalse(self.replaced_copies())
                self.assertEqual(list((self.root / "scratch").iterdir()), [])
        self.assertIn("IMPORT OK", self.import_archive(*self.acknowledge, "--replace-state"))
        self.assertFalse((self.target / "newer-note").exists())
        (copy,) = self.replaced_copies()
        self.assertTrue(copy.with_name(copy.name + ".sha256").is_file())
        self.assertEqual(stat.S_IMODE(copy.stat().st_mode), 0o600)
        self.assertNotIn(b"FAKE-NOT-A-REAL-SECRET", copy.read_bytes())
        self.assertIn("RESTORE TEST OK", self.restore_test(copy))
        members = self.copy_members(copy)
        self.assertEqual(members["state/newer-note"], b"written after the first import")
        self.assertIn(b"FAKE-NOT-A-REAL-SECRET", members["env/pisobot.env"])

    def test_replacing_newer_state_with_an_older_archive_needs_an_explicit_flag(self):
        self.export("--final")
        self.import_archive(*self.acknowledge)
        newer = "2999-01-01T00:00:00+00:00"
        with contextlib.closing(sqlite3.connect(self.target / "pisobot.db")) as db:
            db.execute("CREATE TABLE meta (name TEXT PRIMARY KEY, value TEXT)")
            db.execute("INSERT INTO meta VALUES ('watch_last_success', ?)", (newer,))
            db.commit()
        output = self.import_archive(*self.acknowledge, "--replace-state", success=False)
        self.assertIn("newer than the archive", output)
        self.assertFalse((self.target / ".import-in-progress").exists())
        self.assertFalse((self.root / "previous").exists())
        self.assertEqual(list((self.root / "scratch").iterdir()), [])
        with contextlib.closing(sqlite3.connect(self.target / "pisobot.db")) as db:
            self.assertEqual(db.execute("SELECT value FROM meta").fetchall(), [(newer,)])
        output = self.import_archive(*self.acknowledge, "--replace-state", "--allow-older-archive")
        self.assertIn("IMPORT OK", output)
        self.assertIn("WARNING: replacing newer state", output)
        (copy,) = self.replaced_copies()
        kept = self.root / "kept.db"
        kept.write_bytes(self.copy_members(copy)["state/pisobot.db"])
        with contextlib.closing(sqlite3.connect(kept)) as db:
            self.assertEqual(db.execute("SELECT value FROM meta").fetchall(), [(newer,)])

    def test_replace_flags_are_only_accepted_where_they_apply(self):
        self.export("--final")
        self.import_archive(*self.acknowledge, "--allow-older-archive", success=False)
        self.import_archive("--restore-test", "--replace-state", success=False)
        self.assertEqual(list(self.target.iterdir()), [])

    def test_online_export_only_allows_restore_drill(self):
        self.export()
        self.assertFalse((self.source / "HANDED-OFF-TO-PI").exists())
        self.assertIn("RESTORE TEST OK", self.import_archive("--restore-test"))
        self.assertIn("quiesced", self.import_archive(*self.acknowledge, success=False))
        self.assertEqual(list(self.target.iterdir()), [])

    def test_quiesced_backup_requires_explicit_recovery(self):
        self.export("--quiesced")
        self.assertFalse((self.source / "HANDED-OFF-TO-PI").exists())
        self.import_archive(*self.acknowledge, success=False)
        self.assertIn("IMPORT OK", self.import_archive("--recovery", *self.acknowledge))

    def test_corrupt_ciphertext_rejected_before_destination_changes(self):
        self.export("--final")
        with self.archive.open("ab") as output:
            output.write(b"corruption")
        self.assertIn("SHA256 mismatch", self.import_archive(*self.acknowledge, success=False))
        self.assertEqual(list(self.target.iterdir()), [])

    def test_wrong_identity_rejected_before_destination_changes(self):
        self.export("--final")
        other = self.root / "other-identity"
        subprocess.run(["age-keygen", "-o", str(other)], check=True, capture_output=True)
        other.chmod(0o600)
        self.invoke("pi-import.sh", "--archive", str(self.archive), *self.acknowledge,
                    importing=True, success=False, extra_env={"PISOBOT_AGE_IDENTITY": str(other)})
        self.assertEqual(list(self.target.iterdir()), [])

    def test_wrong_host_and_unsafe_test_path_refused_even_for_dry_run(self):
        self.invoke("vps-export.sh", "--archive", str(self.archive), "--dry-run", success=False,
                    extra_env={"PISOBOT_EXPECT_HOST": "not-this-host"})
        self.invoke("vps-export.sh", "--archive", str(self.archive), "--dry-run", success=False,
                    extra_env={"PISOBOT_EXPECT_ARCH": "not-this-architecture"})
        self.invoke("vps-export.sh", "--archive", str(self.archive), "--dry-run", success=False,
                    extra_env={"PISOBOT_STATE_DIR": "/var/lib/pisobot"})
        self.assertFalse(self.archive.exists())

    def test_dry_runs_make_no_changes(self):
        before = sorted(p.relative_to(self.root) for p in self.root.rglob("*"))
        self.export("--final", "--dry-run")
        self.import_archive(*self.acknowledge, "--dry-run")
        self.invoke("nightly-backup.sh", "--dry-run", extra_env={
            "PISOBOT_BACKUP_TARGET": "unused.invalid", "PISOBOT_BACKUP_DIR": "/unused"})
        self.assertEqual(before, sorted(p.relative_to(self.root) for p in self.root.rglob("*")))

    def test_locked_source_and_output_overwrite_refused(self):
        with (self.source / "watch.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.invoke("vps-export.sh", "--archive", str(self.archive), "--final", success=False)
        self.assertFalse((self.source / "HANDED-OFF-TO-PI").exists())
        self.export("--final")
        original = self.archive.read_bytes()
        self.invoke("vps-export.sh", "--archive", str(self.archive), "--final", success=False)
        self.assertEqual(self.archive.read_bytes(), original)

    def test_final_handoff_and_verified_rollback(self):
        self.export("--final")
        marker = self.source / "HANDED-OFF-TO-PI"
        self.assertTrue(marker.is_file())
        self.assertNotIn("state/HANDED-OFF-TO-PI", self.copy_members(self.archive))
        target_marker = self.target / "HANDED-OFF-TO-PI"
        target_marker.write_text("Old host must stay fenced\n")
        (self.app / "fail-scan").touch()
        self.invoke("pi-import.sh", "--archive", str(self.archive), *self.acknowledge,
                    importing=True, success=False, extra_env={"PISOBOT_HOST_ROLE": "vps"})
        self.assertTrue(target_marker.is_file())
        self.assertTrue((self.target / ".import-in-progress").is_file())
        (self.app / "fail-scan").unlink()
        (self.target / ".import-in-progress").unlink()
        self.invoke("pi-import.sh", "--archive", str(self.archive), *self.acknowledge, "--replace-state",
                    importing=True, extra_env={"PISOBOT_HOST_ROLE": "vps"})
        self.assertFalse(target_marker.exists())
        self.assertFalse((self.target / ".import-in-progress").exists())
        self.assertTrue(marker.is_file())
        target_marker.write_text("Pi previously handed state back\n")
        self.invoke("pi-import.sh", "--archive", str(self.archive), *self.acknowledge, "--replace-state",
                    importing=True, extra_env={"PISOBOT_HOST_ROLE": "pi"})
        self.assertFalse(target_marker.exists())

    def test_missing_export_lock_is_not_created(self):
        (self.source / "run.lock").unlink()
        output = self.invoke("vps-export.sh", "--archive", str(self.archive), "--final", success=False)
        self.assertIn("run.lock is missing", output)
        self.assertFalse((self.source / "run.lock").exists())
        self.assertFalse(self.archive.exists())
        self.assertFalse((self.source / "HANDED-OFF-TO-PI").exists())

    def test_unsafe_environment_is_rejected_before_install(self):
        with (self.root / "source.env").open("a") as stream:
            stream.write("PYTHONPATH=/synthetic/injected-code\n")
        self.export("--final")
        output = self.import_archive(*self.acknowledge, success=False)
        self.assertIn("Environment variable PYTHONPATH", output)
        self.assertNotIn("/synthetic/injected-code", output)
        self.assertEqual([], list(self.target.iterdir()))
        self.assertEqual([], list((self.root / "scratch").iterdir()))

    def test_wrong_source_and_commit_rejected(self):
        self.export("--final")
        self.import_archive("--source-stopped", "another-host", success=False)
        subprocess.run(["git", "-C", str(self.app), "-c", "user.name=Kit test", "-c", "user.email=test@example.invalid",
                        "commit", "--allow-empty", "--quiet", "-m",
                        "Different commit\n\nCo-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"],
                       check=True, capture_output=True)
        self.import_archive(*self.acknowledge, success=False)
        self.assertEqual(list(self.target.iterdir()), [])

    def test_failed_tests_or_scan_keep_the_marker_and_scrub_plaintext_but_not_the_prior_state(self):
        self.export("--final")
        self.import_archive(*self.acknowledge)
        scratch = self.root / "scratch"
        for count, failure in enumerate(("fail-unit", "fail-scan"), start=1):
            with self.subTest(failure=failure):
                (self.target / "prior-only").write_text(f"only in the state before attempt {count}")
                (self.app / failure).touch()
                known = set(self.replaced_copies())
                output = self.import_archive(*self.acknowledge, "--replace-state", success=False)
                self.assertIn("IMPORT FAILED", output)
                self.assertTrue((self.target / ".import-in-progress").exists())
                self.assertFalse((self.target / "prior-only").exists())
                jobs = list(scratch.glob("job.*"))
                self.assertEqual(len(jobs), count)
                for job in jobs:
                    self.assertLessEqual({path.name for path in job.iterdir()}, {"unit-tests.log", "dry-run.log"})
                self.assertTrue(any((job / "unit-tests.log").is_file() for job in jobs))
                for path in scratch.rglob("*"):
                    self.assertFalse(path.is_file() and b"FAKE-NOT-A-REAL-SECRET" in path.read_bytes(), path)
                self.assertEqual(list(scratch.rglob("prior-only")), [])
                # The replaced state survives only as the encrypted copy.
                copy, = set(self.replaced_copies()) - known
                self.assertIn("RESTORE TEST OK", self.restore_test(copy))
                self.assertEqual(self.copy_members(copy)["state/prior-only"].decode(),
                                 f"only in the state before attempt {count}")
                self.assertIn(str(copy), output)
                (self.app / failure).unlink()
                # Exercise the documented acknowledgement after inspecting a failed job.
                (self.target / ".import-in-progress").unlink()
        self.import_archive(*self.acknowledge, "--replace-state")
        self.assertEqual(len(self.replaced_copies()), 3)

    def test_failure_without_replacing_anything_leaves_no_plaintext(self):
        self.export("--final")
        (self.app / "fail-scan").touch()
        self.import_archive(*self.acknowledge, success=False)
        scratch = self.root / "scratch"
        for path in scratch.rglob("*"):
            self.assertFalse(path.is_file() and b"FAKE-NOT-A-REAL-SECRET" in path.read_bytes(), path)
        self.assertEqual({path.name for job in scratch.glob("job.*") for path in job.iterdir()},
                         {"unit-tests.log", "dry-run.log"})
        self.assertFalse((self.root / "previous").exists())

    def test_archive_traversal_and_links_rejected(self):
        for name, kind in (("../escaped", tarfile.REGTYPE), ("state/link", tarfile.SYMTYPE)):
            with self.subTest(name=name):
                stream = io.BytesIO()
                with tarfile.open(fileobj=stream, mode="w:gz") as archive:
                    member = tarfile.TarInfo(name)
                    member.type = kind
                    member.linkname = "/etc/passwd" if kind == tarfile.SYMTYPE else ""
                    archive.addfile(member, io.BytesIO())
                subprocess.run(["age", "--encrypt", "-R", str(self.root / "recipient"), "-o", str(self.archive)],
                               input=stream.getvalue(), capture_output=True, check=True)
                self.archive.chmod(0o600)
                Path(str(self.archive) + ".sha256").write_text(
                    hashlib.sha256(self.archive.read_bytes()).hexdigest() + "\n")
                self.import_archive("--restore-test", success=False)
                self.assertFalse((self.root / "escaped").exists())


if __name__ == "__main__":
    unittest.main()
