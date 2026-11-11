import datetime as dt
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from pisobot import bot, dashboard


NOW = dt.datetime(2026, 11, 11, 12, tzinfo=dt.timezone.utc)


class DashboardHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []
        self.script = []
        self.in_script = False

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))
        if tag == "script":
            self.in_script = True

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_script = False

    def handle_data(self, data):
        if self.in_script:
            self.script.append(data)


def fixture_db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE saved (key TEXT PRIMARY KEY, info TEXT NOT NULL);
        CREATE TABLE adverts (
            key TEXT PRIMARY KEY, url TEXT, address TEXT, bedrooms INTEGER, rent REAL,
            lat REAL, lng REAL, first_seen TEXT, last_seen TEXT, status TEXT, missed INTEGER, data TEXT
        );
        CREATE TABLE observations (id INTEGER PRIMARY KEY, key TEXT, at TEXT, rent REAL, status TEXT);
        CREATE TABLE verdicts (chat_id TEXT, key TEXT, outcome TEXT, rent REAL, all_in REAL,
            travel_minutes INTEGER, PRIMARY KEY(chat_id, key));
        CREATE TABLE pipeline (key TEXT PRIMARY KEY, stage TEXT, url TEXT, address TEXT);
    """)
    return db


def add_flat(db, key, status, last_seen, missed=0, address="Flat", rent=1500):
    db.execute("INSERT INTO saved VALUES (?, ?)", (key, json.dumps({
        "url": "https://example.test/flat", "address": address, "lat": 51.5, "lng": -0.1, "cost": rent + 100,
    })))
    db.execute("INSERT INTO adverts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (
        key, "https://example.test/flat", address, 2, rent, 51.5, -0.1,
        (NOW - dt.timedelta(days=12)).isoformat(), last_seen, status, missed,
        json.dumps({"floor_area_m2": 60, "image": "https://example.test/photo.jpg"}),
    ))
    db.execute("INSERT INTO verdicts VALUES (?, ?, 'matched', ?, ?, ?)",
               ("101", key, rent, rent + 100, 25))


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.db = fixture_db()
        self.addCleanup(self.db.close)

    def test_availability_statuses_and_staleness_boundary(self):
        add_flat(self.db, "a", "active", (NOW - dt.timedelta(hours=24)).isoformat())
        add_flat(self.db, "b", "active", (NOW - dt.timedelta(hours=24, seconds=1)).isoformat())
        add_flat(self.db, "c", "unseen", (NOW - dt.timedelta(days=4)).isoformat(), missed=2)
        page = dashboard.build("101", self.db, NOW)
        self.assertIn("Seen on the portal at", page)
        self.assertIn("Last checked at", page)
        self.assertIn("complete sweeps", page)
        self.assertIn("1 available", page)
        self.assertIn("1 no longer available", page)
        self.assertIn("1 unknown", page)

    def test_untrusted_advert_text_is_escaped(self):
        add_flat(self.db, "unsafe", "active", NOW.isoformat(), address='<img src=x onerror="alert(1)">')
        image = 'https://example.test/photo.jpg" onerror="alert(1)'
        self.db.execute("UPDATE adverts SET data=? WHERE key='unsafe'", (json.dumps({"image": image}),))
        page = dashboard.build("101", self.db, NOW)
        self.assertIn("&lt;img src=x onerror=&quot;alert(1)&quot;&gt;", page)
        self.assertNotIn('<img src=x onerror="alert(1)">', page)
        parsed = DashboardHTML()
        parsed.feed(page)
        attrs = next(attrs for tag, attrs in parsed.tags if tag == "img")
        self.assertEqual(attrs["data-remote-src"], image)
        self.assertNotIn("onerror", attrs)

    def test_external_content_is_disclosed_and_inert_in_initial_html(self):
        add_flat(self.db, "a", "active", NOW.isoformat())
        page = dashboard.build("101", self.db, NOW)
        parsed = DashboardHTML()
        parsed.feed(page)
        images = [attrs for tag, attrs in parsed.tags if tag == "img"]
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0]["data-remote-src"], "https://example.test/photo.jpg")
        self.assertNotIn("src", images[0])
        self.assertNotIn("srcset", images[0])
        self.assertIn("hidden", images[0])
        self.assertFalse(any(tag == "link" or (tag == "script" and "src" in attrs)
                             for tag, attrs in parsed.tags))
        button = next(attrs for tag, attrs in parsed.tags if tag == "button" and attrs.get("id") == "load-external")
        self.assertIn("disabled", button)
        for text in ("External content is off", "IP address and access time", "Photo hosts:",
                     "unpkg.com", "OpenStreetMap tile servers", "*.tile.openstreetmap.org", "redirect destinations"):
            self.assertIn(text, page)
        self.assertIn('id="map-fallback"', page)
        self.assertIn("Flat", page)

    @unittest.skipUnless(shutil.which("node"), "Node is needed for the offline JavaScript check")
    def test_external_requests_only_start_after_opt_in(self):
        add_flat(self.db, "a", "active", NOW.isoformat())
        add_flat(self.db, "b", "active", NOW.isoformat())
        self.db.execute("UPDATE adverts SET data=? WHERE key='b'",
                        (json.dumps({"image": "https://images.other.test/photo.jpg?x=1&y=2"}),))
        parsed = DashboardHTML()
        parsed.feed(dashboard.build("101", self.db, NOW))
        images = [attrs["data-remote-src"] for tag, attrs in parsed.tags if tag == "img"]
        runner = Path(__file__).with_name("dashboard_privacy_check.js")
        result = subprocess.run(
            [shutil.which("node"), str(runner)],
            input=json.dumps({"script": "".join(parsed.script), "images": images}),
            text=True, capture_output=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_verdicts_are_scoped_to_the_requested_person(self):
        add_flat(self.db, "a", "active", NOW.isoformat())
        self.db.execute("UPDATE verdicts SET travel_minutes=40 WHERE chat_id='101'")
        self.db.execute("INSERT INTO verdicts VALUES ('202', 'a', 'matched', 1500, 1600, 12)")
        first = dashboard.build("101", self.db, NOW)
        second = dashboard.build("202", self.db, NOW)
        self.assertIn("40 min", first)
        self.assertIn("12 min", second)

    def test_profile_database_is_read_only_and_does_not_include_other_people(self):
        with tempfile.TemporaryDirectory() as state:
            root = Path(state)
            primary = sqlite3.connect(root / "pisobot.db")
            primary.executescript("""
                CREATE TABLE saved (key TEXT PRIMARY KEY, info TEXT NOT NULL);
                CREATE TABLE users (chat_id TEXT PRIMARY KEY, name TEXT);
                CREATE TABLE adverts (key TEXT PRIMARY KEY, status TEXT);
                INSERT INTO saved VALUES ('owner:flat', '{}');
                INSERT INTO users VALUES ('202', 'Alice');
            """)
            primary.commit()
            primary.close()
            profile_dir = root / "users" / "202"
            profile_dir.mkdir(parents=True)
            profile = sqlite3.connect(profile_dir / "pisobot.db")
            profile.execute("CREATE TABLE saved (key TEXT PRIMARY KEY, info TEXT NOT NULL)")
            profile.execute("INSERT INTO saved VALUES ('alice:flat', ?)", (json.dumps({"address": "Alice flat"}),))
            profile.commit()
            profile.close()
            with patch.dict(os.environ, {"PISOBOT_STATE_DIR": state, "TELEGRAM_CHAT_ID": "101"}):
                db = dashboard._database("202")
                try:
                    self.assertIn("Alice", dashboard.build("202", db, NOW))
                    page = dashboard.build("202", db, NOW)
                    self.assertIn("Alice flat", page)
                    self.assertNotIn("owner:flat", page)
                    with self.assertRaises(sqlite3.OperationalError):
                        db.execute("INSERT INTO saved VALUES ('not:allowed', '{}')")
                finally:
                    db.close()

    def test_html_size_limit_is_enforced(self):
        add_flat(self.db, "a", "active", NOW.isoformat())
        with patch.object(dashboard, "MAX_HTML_BYTES", 100):
            with self.assertRaises(dashboard.DashboardError):
                dashboard.build("101", self.db, NOW)

    def test_flag_off_leaves_command_unhandled(self):
        client = type("Client", (), {"call": lambda *args, **kwargs: None})()
        handler = bot.Handler("101", lambda *args: True, client)
        with patch.dict("os.environ", {}, clear=True):
            handler.command("/dashboard")
        self.assertFalse(dashboard.enabled())
