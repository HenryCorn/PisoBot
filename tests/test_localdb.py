from concurrent.futures import ThreadPoolExecutor
import importlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from pisobot import cache, d1, localdb, prefs


class LocalStorageTests(unittest.TestCase):
    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory(prefix="pisobot-test-localdb-"))
        self.state = Path(self.directory).resolve() / "state"
        self.enterContext(patch.dict(os.environ, {
            "STORAGE": "local", "PISOBOT_STATE_DIR": str(self.state),
        }, clear=True))
        self.post = self.enterContext(patch.object(
            d1.net.session, "post", side_effect=AssertionError("Unexpected network request")))
        self.enterContext(patch.object(cache, "PATH", str(self.state / "cache.json")))
        self.enterContext(patch.object(cache, "_data", None))

    def test_preferences_roundtrip_reopen_and_concurrent_choices(self):
        self.assertTrue(d1.configured())
        self.assertEqual(prefs.load()["saved"], {})
        d1.query("INSERT INTO saved VALUES (?, ?)", "stb:1", json.dumps({"cost": 1200}))
        d1.query("INSERT INTO hidden VALUES (?)", "stb:2")
        d1.query("INSERT INTO hidden_dev VALUES (?, ?)", 51.5, -0.1)
        p = prefs.load()
        self.assertEqual(p["hidden"], ["stb:2"])
        self.assertEqual(p["hidden_dev"], [[51.5, -0.1]])
        p["saved"]["stb:1"]["cost"] = 1100
        p["last_report"] = "2026-11-08T19:00"
        d1.query("INSERT INTO saved VALUES (?, ?)", "stb:3", "{}")
        prefs.save(p)
        reopened = prefs.load()
        self.assertEqual(reopened["saved"], {"stb:1": {"cost": 1100}, "stb:3": {}})
        self.assertEqual(reopened["last_report"], p["last_report"])
        # A concurrent unsave must not be undone by this run's cost update.
        d1.query("DELETE FROM saved WHERE key = ?", "stb:1")
        p["saved"]["stb:1"]["cost"] = 1000
        prefs.save(p)
        self.assertEqual(prefs.load()["saved"], {"stb:3": {}})
        del reopened["saved"]["stb:3"]
        prefs.save(reopened)
        self.assertEqual(prefs.load()["saved"], {})
        with sqlite3.connect(self.state / "pisobot.db") as db:
            self.assertEqual(db.execute("SELECT key FROM hidden").fetchall(), [("stb:2",)])
        db.close()
        self.post.assert_not_called()

    def test_publish_uses_json1_and_upserts_multiple_chunks(self):
        known = {
            "stb:1": {"lat": 51.5, "lng": -0.1, "cost": 1000,
                      "url": "https://example.com/1", "address": "O'Brien Court"},
            "stb:2": {"cost": 1100},
            "stb:3": {"cost": 1200},
        }
        prefs.publish(known, chunk=2)
        known["stb:1"]["cost"] = 900
        prefs.publish(known, chunk=2)
        rows = d1.query("SELECT * FROM listings ORDER BY key")
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0], {"sid": prefs.sid("stb:1"), "key": "stb:1", **known["stb:1"]})
        self.assertIsNone(rows[1]["lat"])
        self.assertEqual(d1.query("SELECT json_extract(value, '$.a') AS a FROM json_each(?)",
                                 '[{"a": 5}]'), [{"a": 5}])

    def test_feedback_and_restart(self):
        d1.query("INSERT INTO feedback VALUES (?, ?, ?, ?)",
                 "stb:1", "hide", "too_expensive", "2026-11-08T19:00")
        d1.query("INSERT INTO saved VALUES (?, ?)", "stb:1", "{}")
        d1.query("INSERT INTO hidden VALUES (?)", "stb:2")
        d1.query("INSERT INTO hidden_dev VALUES (?, ?)", 51.5, -0.1)
        d1.query("INSERT INTO meta VALUES (?, ?)", "last_report", "yesterday")
        expected = [{"key": "stb:1", "action": "hide", "reason": "too_expensive"}]
        self.assertEqual(prefs.load()["feedback"], expected)
        prefs.restart()
        p = prefs.load()
        self.assertEqual(p["hidden"], [])
        self.assertEqual(p["hidden_dev"], [])
        self.assertNotIn("last_report", p)
        self.assertEqual(p["feedback"], expected)
        self.assertEqual(p["saved"], {"stb:1": {}})
        prefs.restart(full=True)
        self.assertEqual(prefs.load()["saved"], {})
        self.assertEqual(prefs.load()["feedback"], [])

    def test_concurrent_initialization_and_writes(self):
        def increment(_):
            return d1.query("INSERT INTO meta VALUES ('counter', '1') "
                            "ON CONFLICT(name) DO UPDATE SET value = value + 1")

        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(list(pool.map(increment, range(32))), [[]] * 32)
        self.assertEqual(d1.query("SELECT value FROM meta WHERE name = 'counter'"),
                         [{"value": "32"}])
        self.assertEqual(d1.query("PRAGMA journal_mode"), [{"journal_mode": "wal"}])
        self.assertGreaterEqual(d1.query("PRAGMA busy_timeout")[0]["timeout"], 5000)

    def test_connections_close_on_success_and_failure(self):
        connections = []
        connect = sqlite3.connect

        def tracked(*args, **kwargs):
            db = connect(*args, **kwargs)
            connections.append(db)
            return db

        with patch.object(localdb.sqlite3, "connect", side_effect=tracked):
            d1.query("INSERT INTO hidden VALUES (?)", "stb:1")
            with self.assertRaises(sqlite3.IntegrityError):
                d1.query("INSERT INTO hidden VALUES (?)", "stb:1")
        for db in connections:
            with self.assertRaises(sqlite3.ProgrammingError):
                db.execute("SELECT 1")
        self.assertEqual(d1.query("SELECT key FROM hidden"), [{"key": "stb:1"}])

    def test_wal_initialization_retries_only_busy_errors(self):
        busy = sqlite3.OperationalError("database is locked")
        busy.sqlite_errorcode = sqlite3.SQLITE_BUSY
        db = Mock()
        db.execute.side_effect = [busy, None]
        with patch.object(localdb.time, "sleep") as sleep:
            localdb._enable_wal(db)
        self.assertEqual(db.execute.call_count, 2)
        sleep.assert_called_once()
        db.execute.side_effect = sqlite3.OperationalError("disk I/O error")
        with self.assertRaisesRegex(sqlite3.OperationalError, "disk I/O error"):
            localdb._enable_wal(db)

    def test_corrupt_database_is_not_replaced(self):
        self.state.mkdir()
        path = self.state / "pisobot.db"
        path.write_bytes(b"not a sqlite database")
        with self.assertRaises(sqlite3.DatabaseError):
            prefs.load()
        self.assertEqual(path.read_bytes(), b"not a sqlite database")

    def test_json1_unavailable_falls_back_to_parameterized_upserts(self):
        connect = sqlite3.connect

        class NoJSON(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                if "json_each" in sql:
                    raise sqlite3.OperationalError("no such table: json_each")
                return super().execute(sql, parameters)

        with patch.object(localdb.sqlite3, "connect",
                          side_effect=lambda *a, **kw: connect(*a, factory=NoJSON, **kw)):
            prefs.publish({"stb:1": {"address": "O'Brien Court"}, "stb:2": {}}, chunk=2)
            prefs.publish({"stb:1": {"cost": 900}})
        self.assertEqual(d1.query("SELECT key, cost FROM listings ORDER BY key"),
                         [{"key": "stb:1", "cost": 900}, {"key": "stb:2", "cost": None}])

    def test_unrelated_publish_errors_are_not_hidden(self):
        connect = sqlite3.connect

        class BrokenSQL(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                if "json_each" in sql:
                    raise sqlite3.OperationalError("no such column: typo")
                return super().execute(sql, parameters)

        with patch.object(localdb.sqlite3, "connect",
                          side_effect=lambda *a, **kw: connect(*a, factory=BrokenSQL, **kw)):
            with self.assertRaisesRegex(sqlite3.OperationalError, "no such column"):
                prefs.publish({"stb:1": {"cost": 900}})
        self.assertEqual(d1.query("SELECT * FROM listings"), [])

    def test_json1_fallback_rolls_back_a_failed_chunk(self):
        connect = sqlite3.connect

        class NoJSON(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                if "json_each" in sql:
                    raise sqlite3.OperationalError("no such function: json_extract")
                return super().execute(sql, parameters)

        with patch.object(localdb.sqlite3, "connect",
                          side_effect=lambda *a, **kw: connect(*a, factory=NoJSON, **kw)):
            with self.assertRaises(sqlite3.IntegrityError):
                d1.query(localdb._PUBLISH_SQL, json.dumps([
                    {"sid": "first", "key": "stb:1"}, {"sid": "second"},
                ]))
        self.assertEqual(d1.query("SELECT * FROM listings"), [])

    def test_storage_routing_keeps_default_cloud_request(self):
        for storage in (None, "cloud"):
            with self.subTest(storage=storage):
                env = {name: f" {i} " for i, name in enumerate(d1.NAMES)}
                if storage is not None:
                    env["STORAGE"] = storage
                response = Mock()
                response.json.return_value = {"success": True, "result": [{"results": [{"key": "x"}]}]}
                with patch.dict(os.environ, env, clear=True), \
                        patch.object(d1.net.session, "post", return_value=response) as post, \
                        patch.object(localdb, "query", side_effect=AssertionError("Local routing")):
                    self.assertTrue(d1.configured())
                    self.assertEqual(d1.query("SELECT ? AS key", "x"), [{"key": "x"}])
                    post.assert_called_once_with(
                        d1.URL.format(account="0", database="1"),
                        json={"sql": "SELECT ? AS key", "params": ["x"]},
                        headers={"Authorization": "Bearer 2"}, timeout=30, verify=d1.net.VERIFY)

    def test_unconfigured_cloud_is_unchanged(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(d1.configured())
            with self.assertRaisesRegex(RuntimeError, "GitHub Actions secrets"):
                d1.query("SELECT 1")
        self.post.assert_not_called()

    def test_invalid_explicit_storage_errors(self):
        for value in ("", "sqlite", "LOCAL", " local "):
            with self.subTest(value=value), patch.dict(os.environ, {"STORAGE": value}):
                with self.assertRaisesRegex(ValueError, "Invalid STORAGE"):
                    d1.configured()
                with self.assertRaisesRegex(ValueError, "Invalid STORAGE"):
                    d1.query("SELECT 1")
        self.post.assert_not_called()

    def test_state_paths_override_and_default(self):
        old_path, old_data = cache.PATH, cache._data
        try:
            for value in (str(self.state), "", None):
                with self.subTest(value=value):
                    env = {"PISOBOT_STATE_DIR": value} if value is not None else {}
                    with patch.dict(os.environ, env, clear=True):
                        importlib.reload(cache)
                        directory = Path(value) if value else localdb.ROOT / "state"
                        self.assertEqual(Path(cache.PATH).resolve(), directory / "cache.json")
                        self.assertEqual(localdb.database_path(), directory / "pisobot.db")
        finally:
            cache.PATH, cache._data = old_path, old_data

    def test_atomic_cache_save_and_reopen(self):
        self.assertIsNone(cache.get("meta", "missing"))
        cache.put("meta", "report", {"count": 3})
        cache.save()
        cache._data = None
        self.assertEqual(cache.get("meta", "report"), {"count": 3})
        self.assertEqual(list(self.state.iterdir()), [Path(cache.PATH)])

    def test_failed_cache_save_preserves_previous_file(self):
        cache.put("meta", "report", 1)
        cache.save()
        original = Path(cache.PATH).read_bytes()
        cache.put("meta", "report", 2)
        with patch.object(cache.os, "replace", side_effect=OSError("disk failure")):
            with self.assertRaisesRegex(OSError, "disk failure"):
                cache.save()
        self.assertEqual(Path(cache.PATH).read_bytes(), original)
        self.assertEqual(list(self.state.iterdir()), [Path(cache.PATH)])
        cache.put("meta", "report", object())
        with self.assertRaises(TypeError):
            cache.save()
        self.assertEqual(Path(cache.PATH).read_bytes(), original)
        self.assertEqual(list(self.state.iterdir()), [Path(cache.PATH)])

    def test_local_corrupt_cache_errors_without_overwriting(self):
        self.state.mkdir()
        path = Path(cache.PATH)
        for content in (b"{broken", b"\xff", b"[]", b'{"meta": []}'):
            with self.subTest(content=content):
                path.write_bytes(content)
                with self.assertRaisesRegex(RuntimeError, "Invalid local cache"):
                    cache.get("meta", "report")
                self.assertIsNone(cache._data)
                cache.save()
                self.assertEqual(path.read_bytes(), content)

    def test_cloud_corrupt_cache_retains_historical_empty_fallback(self):
        self.state.mkdir()
        Path(cache.PATH).write_text("{broken", encoding="utf-8")
        with patch.dict(os.environ, {"STORAGE": "cloud"}):
            self.assertIsNone(cache.get("meta", "report"))
            self.assertEqual(cache._data, {})


if __name__ == "__main__":
    unittest.main()
