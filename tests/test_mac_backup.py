import io
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from deploy import mac_backup


class MacBackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.addCleanup(self.make_db(self.state / "pisobot.db").close)
        (self.state / "seen.json").write_text('{"seen": 1}\n')
        (self.state / "prefs.json").write_text('{"saved": []}\n')

    @staticmethod
    def make_db(path):
        db = sqlite3.connect(path)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA wal_autocheckpoint=0")
        db.execute("CREATE TABLE prefs (name TEXT PRIMARY KEY, value TEXT)")
        db.execute("INSERT INTO prefs VALUES ('rent', '1600')")
        db.commit()
        return db

    def test_live_snapshot_captures_wal_and_verifies_state_counts(self):
        user_db = self.state / "users" / "123" / "pisobot.db"
        user_db.parent.mkdir(parents=True)
        writer = self.make_db(user_db)
        self.addCleanup(writer.close)
        (self.state / "users" / "123" / "seen.json").write_text('{"user": true}\n')
        wal = self.state / "users" / "123" / "pisobot.db-wal"
        self.assertTrue(wal.exists())

        snapshot = self.root / "snapshot"
        manifest = mac_backup.snapshot_state(self.state, snapshot)
        self.assertTrue(wal.exists())
        self.assertEqual(1, manifest["database_rows"]["pisobot.db"]["prefs"])
        self.assertEqual(1, manifest["database_rows"]["users/123/pisobot.db"]["prefs"])

        archive = io.BytesIO()
        mac_backup.write_tar(snapshot, archive)
        restored = self.root / "restore"
        counts = mac_backup.verify_tar(io.BytesIO(archive.getvalue()), restored)
        self.assertEqual((2, 2, 3), counts)
        self.assertEqual('{"user": true}\n', (restored / "state/users/123/seen.json").read_text())

    def test_verifier_rejects_modified_snapshot_data(self):
        snapshot = self.root / "snapshot"
        mac_backup.snapshot_state(self.state, snapshot)
        (snapshot / "state/seen.json").write_text('{"changed": true}\n')
        archive = io.BytesIO()
        mac_backup.write_tar(snapshot, archive)
        with self.assertRaisesRegex(ValueError, "hashes do not match"):
            mac_backup.verify_tar(io.BytesIO(archive.getvalue()), self.root / "restore")

    def test_state_symlinks_are_refused(self):
        (self.state / "unsafe").symlink_to(self.state / "seen.json")
        with self.assertRaisesRegex(ValueError, "symlink"):
            mac_backup.snapshot_state(self.state, self.root / "snapshot")

    def test_retention_removes_only_expired_backup_files(self):
        expired = self.root / "pisobot-20200101T000000Z.tar.gz.age"
        expired.write_bytes(b"encrypted")
        Path(f"{expired}.sha256").write_text("digest\n")
        unrelated = self.root / "other.tar.gz.age"
        unrelated.write_bytes(b"keep")
        os.utime(expired, (1, 1))

        self.assertEqual(1, mac_backup.prune(self.root, 14))
        self.assertFalse(expired.exists())
        self.assertFalse(Path(f"{expired}.sha256").exists())
        self.assertTrue(unrelated.exists())


if __name__ == "__main__":
    unittest.main()
