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
        self.env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
                        PISOBOT_TEST_ROOT=str(self.root),
                        PISOBOT_EXPECT_HOST=subprocess.check_output(["hostname", "-s"], text=True).strip(),
                        PISOBOT_EXPECT_ARCH=platform.machine(),
                        PISOBOT_STATE_DIR=str(self.source),
                        PISOBOT_ENV_FILE=str(self.root / "source.env"),
                        PISOBOT_APP_DIR=str(self.app),
                        PISOBOT_VENV_DIR=str(self.root / "venv"),
                        PISOBOT_TMP_DIR=str(self.root / "scratch"),
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
        self.import_archive(*self.acknowledge)
        self.assertFalse((self.target / "stale").exists())
        self.assertEqual(list((self.root / "scratch").iterdir()), [])

    def test_online_export_only_allows_restore_drill(self):
        self.export()
        self.assertIn("RESTORE TEST OK", self.import_archive("--restore-test"))
        self.assertIn("quiesced", self.import_archive(*self.acknowledge, success=False))
        self.assertEqual(list(self.target.iterdir()), [])

    def test_quiesced_backup_requires_explicit_recovery(self):
        self.export("--quiesced")
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
        self.export("--final")
        original = self.archive.read_bytes()
        self.invoke("vps-export.sh", "--archive", str(self.archive), "--final", success=False)
        self.assertEqual(self.archive.read_bytes(), original)

    def test_wrong_source_and_commit_rejected(self):
        self.export("--final")
        self.import_archive("--source-stopped", "another-host", success=False)
        subprocess.run(["git", "-C", str(self.app), "-c", "user.name=Kit test", "-c", "user.email=test@example.invalid",
                        "commit", "--allow-empty", "--quiet", "-m",
                        "Different commit\n\nCo-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"],
                       check=True, capture_output=True)
        self.import_archive(*self.acknowledge, success=False)
        self.assertEqual(list(self.target.iterdir()), [])

    def test_failed_tests_or_scan_keep_recovery_state_and_marker(self):
        self.export("--final")
        for failure in ("fail-unit", "fail-scan"):
            with self.subTest(failure=failure):
                (self.app / failure).touch()
                output = self.import_archive(*self.acknowledge, success=False)
                self.assertIn("IMPORT FAILED", output)
                self.assertTrue((self.target / ".import-in-progress").exists())
                jobs = list((self.root / "scratch").glob("job.*"))
                self.assertTrue(any((job / "restored/previous").is_dir() for job in jobs))
                (self.app / failure).unlink()
                # Exercise the documented acknowledgement after inspecting a failed job.
                (self.target / ".import-in-progress").unlink()
        self.import_archive(*self.acknowledge)

    def test_archive_traversal_and_links_rejected(self):
        for name, kind in (("../escaped", tarfile.REGTYPE), ("state/link", tarfile.SYMTYPE)):
            with self.subTest(name=name):
                stream = io.BytesIO()
                with tarfile.open(fileobj=stream, mode="w:gz") as archive:
                    member = tarfile.TarInfo(name)
                    member.type = kind
                    member.linkname = "/etc/passwd" if kind == tarfile.SYMTYPE else ""
                    archive.addfile(member, io.BytesIO())
                result = subprocess.run(["age", "-R", str(self.root / "recipient")],
                                        input=stream.getvalue(), capture_output=True, check=True)
                self.archive.write_bytes(result.stdout)
                Path(str(self.archive) + ".sha256").write_text(hashlib.sha256(result.stdout).hexdigest() + "\n")
                self.import_archive("--restore-test", success=False)
                self.assertFalse((self.root / "escaped").exists())


if __name__ == "__main__":
    unittest.main()
