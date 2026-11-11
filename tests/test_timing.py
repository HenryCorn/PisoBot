"""G1 (timing only): alert delay measurement, 'Listed N min ago', the OnTheMarket exclusive flag. Includes the independent
reviewer's delivery-order reproductions. Offline: fakes only."""
import collections
import contextlib
import datetime as dt
import json
import multiprocessing
import os
import re
import sqlite3
import subprocess
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from pisobot import archive, cache, config, d1, localdb, main, status, telegram, timing, users, watch
from pisobot.models import Listing
from pisobot.sources import apply_rent_details

from test_users import Base, FRIEND, OWNER, home

ROOT = Path(__file__).resolve().parents[1]
NOW = dt.datetime(2026, 11, 10, 12, 0, tzinfo=dt.timezone.utc)


def iso(minutes_ago, now=NOW):
    return (now - dt.timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")


class TimingBase(Base):
    def setUp(self):
        super().setUp()
        for target in (patch.object(main.enrich, "geocode"), patch.object(main.enrich, "assign_crime_percentiles"),
                       patch.object(main, "passes_area", return_value=None), patch.object(main, "_ensure_photos"),
                       patch.object(telegram, "throttle")):
            target.start()
            self.addCleanup(target.stop)
        self.enterContext(patch.object(main, "passes_basic", side_effect=lambda l, **kw: main._cost_reject(l)))
        self.full = [home(1, 1200)]
        self.enterContext(patch.object(main.SOURCES["rm"], "fetch", side_effect=lambda: list(self.full)))
        self.sent, self.events = [], []
        self.during_send = None
        self.enterContext(patch.object(telegram, "send_listing", side_effect=self._send))
        self.enterContext(patch.object(telegram, "send_text"))
        self.admit({"bills": 100.0})
        self.watcher = watch.Watcher(["rm"])
        timing._buffer.clear()

    def _send(self, l, reason):
        self.sent.append((users.current_chat(), l.key, reason))
        if self.during_send:
            self.during_send()
        return {"result": {"message_id": 1}}

    def baseline(self):
        self.watcher.cycle()
        self.sent.clear()

    def counts(self):
        return collections.Counter(self.sent)


# ---------------------------------------------------------------- the measurement

class MeasurementTests(Base):
    def listing(self, i, source="rm", listed=None):
        l = home(i)
        l.source, l.first_listed_at = source, listed
        return l

    def rec(self, listing, reason="new", via="head", sent_at=NOW, now=NOW):
        return timing.write([timing.capture(listing, reason, via, sent_at)], now=now)

    def test_median_and_p90_per_source_over_seven_days(self):
        for i, delay in enumerate((10, 20, 30, 40, 100), 1):
            l = self.listing(i, listed=iso(delay))
            archive.upsert([l], "head", at=iso(i))
            self.rec(l)
        self.rec(self.listing(9, "otmr"))                                                # no portal timestamp
        old = NOW - dt.timedelta(days=8)
        self.rec(self.listing(10, listed=iso(5, old)), sent_at=old, now=old)           # outside 7 days
        self.rec(self.listing(11, listed=iso(5)), reason="drop")                       # not a new advert
        r = timing.report(now=NOW)
        self.assertEqual(r["rm"]["alerts"], 5)
        self.assertEqual((r["rm"]["portal_median"], r["rm"]["portal_p90"], r["rm"]["portal_n"]), (30, 100, 5))
        self.assertEqual((r["rm"]["fetch_median"], r["rm"]["fetch_p90"]), (3, 5))
        self.assertEqual((r["otmr"]["alerts"], r["otmr"]["portal_n"]), (1, 0))
        self.assertIsNone(r["otmr"]["portal_median"])

    def test_future_portal_timestamp_is_left_out(self):
        self.rec(self.listing(1, listed=iso(-30)))
        self.assertEqual(timing.report(now=NOW)["rm"]["portal_n"], 0)

    def test_nearest_rank_percentile(self):
        self.assertEqual(timing.percentile(list(range(1, 11)), 90), 9)
        self.assertEqual(timing.percentile([5], 90), 5)
        self.assertIsNone(timing.percentile([], 50))

    def test_first_fetch_comes_from_the_archive(self):
        l = self.listing(1, listed=iso(30))
        archive.upsert([l], "head", at=iso(12))
        self.rec(l)
        self.assertEqual(timing.report(now=NOW)["rm"]["fetch_median"], 12)

    def test_recording_never_raises_and_needs_local_storage(self):
        with patch.object(timing, "_db", side_effect=RuntimeError("db down")):
            self.assertEqual(self.rec(self.listing(1)), 0)
        with patch.dict(os.environ, {"STORAGE": "cloud"}):
            self.assertEqual(self.rec(self.listing(2)), 0)
        self.assertEqual(timing.report(now=NOW), {})

    def test_capture_does_no_io(self):
        with patch.object(timing, "_db", side_effect=AssertionError("capture touched the database")):
            row = timing.capture(self.listing(1, listed=iso(5)), "new", "head", NOW)
        self.assertEqual((row["source"], row["via"], row["sent_at"]), ("rm", "head", NOW.isoformat(timespec="seconds")))

    def test_rows_hold_no_person_or_advert_identifier(self):
        self.rec(self.listing(1, listed=iso(5)))
        with timing._db() as db:
            columns = {r[1] for r in db.execute("PRAGMA table_info(alert_timing)")}
        self.assertEqual(columns, {"id", "source", "reason", "via", "listed_at", "first_fetch_at", "sent_at", "exclusive"})

    def test_status_text_shows_the_delay_lines(self):
        for delay in (10, 20, 30):
            self.rec(self.listing(delay, listed=iso(delay)))
        text = "\n".join(timing.status_lines(NOW))
        self.assertIn("Alert delay", text)
        self.assertIn("Rightmove: 3 alerts", text)
        self.assertIn("20 min / 30 min from the portal's listing time (3 timed)", text)
        self.assertNotIn("fast", text.lower())
        with patch.object(timing, "_now", return_value=NOW):
            self.assertIn("Alert delay", status.status_text(NOW))

    def test_status_text_has_no_delay_section_before_any_alert(self):
        self.assertNotIn("Alert delay", status.status_text(NOW))

    def test_timezone_date_only_and_future_timestamps(self):
        self.assertEqual(timing._minutes("2026-07-01T10:00:00+00:00", "2026-07-01T10:00:00+01:00"), 60)
        self.assertIsNone(timing._minutes("2026-07-01T10:00:00+00:00", "2026-07-01"))
        now = dt.datetime.now(dt.timezone.utc)
        for ts in ("2026-11-11", "2026-11-11T03:00:00", (now + dt.timedelta(minutes=1)).isoformat()):
            l = home()
            l.first_listed_at = ts
            self.assertEqual(telegram.listed_ago(l, now), "")


class ListedAgoTests(unittest.TestCase):
    def l(self, ts):
        x = home()
        x.first_listed_at = ts
        return x

    def test_minutes_and_hours(self):
        self.assertEqual(telegram.listed_ago(self.l(iso(40)), NOW), "Listed 40 min ago")
        self.assertEqual(telegram.listed_ago(self.l(iso(0)), NOW), "Listed just now")
        self.assertEqual(telegram.listed_ago(self.l(iso(60)), NOW), "Listed 1 h ago")
        self.assertEqual(telegram.listed_ago(self.l(iso(125)), NOW), "Listed 2 h 5 min ago")

    def test_three_hour_cutoff(self):
        self.assertEqual(telegram.listed_ago(self.l(iso(179)), NOW), "Listed 2 h 59 min ago")
        self.assertEqual(telegram.listed_ago(self.l(iso(180)), NOW), "")

    def test_nothing_without_a_full_timestamp_or_with_a_bad_one(self):
        for ts in (None, "", "2026-11-10", "not a time", iso(-5), "2026-11-10T11:00:00"):
            self.assertEqual(telegram.listed_ago(self.l(ts), NOW), "", ts)

    def test_card_uses_it_only_when_it_applies(self):
        fresh, old = self.l(iso(40)), self.l(iso(500))
        old.first_listed = "2026-11-10"
        self.assertIn("🕒 Listed 40 min ago", telegram.portal_line(fresh, now=NOW))
        self.assertIn("Added today", telegram.portal_line(old, today=NOW.date(), now=NOW))


class ExclusiveFlagTests(unittest.TestCase):
    def test_the_details_flag_is_stored_on_the_listing_when_present(self):
        l = home()
        apply_rent_details(l, {"description": "x", "exclusive": True})
        self.assertIs(l.exclusive, True)
        m = home()
        apply_rent_details(m, {"description": "x"})
        self.assertIsNone(m.exclusive)

    def test_the_flag_is_recorded_with_the_alert_and_never_shown(self):
        l = home()
        l.exclusive = True
        row = timing.capture(l, "new", "head", NOW)
        self.assertEqual(row["exclusive"], 1)
        self.assertNotIn("xclusive", telegram.format_listing(l))

    def test_the_stored_search_snapshot_is_exactly_the_releases(self):
        from pisobot.sources import CACHE_FIELDS
        self.assertNotIn("exclusive", CACHE_FIELDS)


# ---------------------------------------------------------------- the reviewer's delivery-order reproductions

class DeliveryOrderTests(TimingBase):
    def test_timing_is_captured_in_memory_and_written_only_after_receipt_acknowledgement_and_the_run_lock(self):
        self.baseline()
        self.full = [home(1), home(2)]
        events = []
        real_save, real_verdicts = main.save_seen, archive.record_verdicts
        real_capture, real_write = timing.capture, timing.write
        real_locked = main.locked

        @contextlib.contextmanager
        def locked():
            events.append("lock")
            with real_locked():
                yield
            events.append("unlock")

        def save(seen):
            if "rm:2" in seen:
                events.append("receipt")
            return real_save(seen)

        def verdicts(*a, **kw):
            if kw.get("acknowledged"):
                events.append("ack")
            return real_verdicts(*a, **kw)

        def capture(*a, **kw):
            events.append("capture")
            with patch.object(timing, "_db", side_effect=AssertionError("capture did I/O")):
                return real_capture(*a, **kw)

        def write(rows, *a, **kw):
            events.append("write")
            return real_write(rows, *a, **kw)

        self.during_send = lambda: events.append("telegram")
        with patch.object(main, "save_seen", side_effect=save), patch.object(archive, "record_verdicts", side_effect=verdicts), \
                patch.object(timing, "capture", side_effect=capture), patch.object(timing, "write", side_effect=write), \
                patch.object(main, "locked", side_effect=locked):
            self.watcher.cycle()
        self.assertLess(events.index("telegram"), events.index("capture"))
        self.assertLess(events.index("capture"), events.index("receipt"))
        self.assertLess(events.index("receipt"), events.index("ack"))
        self.assertLess(events.index("ack"), events.index("unlock"))
        self.assertLess(events.index("unlock"), events.index("write"))               # after the run lock is released
        self.assertEqual(timing.report()["rm"]["alerts"], 2)

    def test_nothing_is_written_inside_run(self):
        self.baseline()
        self.full = [home(1), home(2)]
        with patch.object(timing, "write", side_effect=AssertionError("timing written inside _run")), \
                patch.object(timing, "_db", side_effect=AssertionError("timing touched the database inside _run")):
            main._run(["rm"], False, False, rentals=True, watch=True, prefetched=(list(self.full), []))
        self.assertEqual(len(timing._buffer), 1)                                       # parked in memory only (one person, one new advert)

    def _contention_crash(self, release_control=False):
        """Telegram accepts a card, another writer holds the database, the sender is killed while waiting."""
        self.baseline()
        self.full = [home(1), home(2)]
        with timing._db():
            pass
        ctx = multiprocessing.get_context("fork")
        accepted, waiting = ctx.Event(), ctx.Event()
        held = []
        real_verdicts = archive.record_verdicts
        release_module = None
        if release_control:
            try:
                source = subprocess.check_output(["git", "-C", str(ROOT), "show", "origin/release-archive-pipeline:pisobot/main.py"],
                                                 text=True, stderr=subprocess.DEVNULL)
            except Exception:
                self.skipTest("release main.py not available from git")
            release_module = types.ModuleType("pisobot._review_release_main")
            release_module.__file__ = str(ROOT / "pisobot" / "main.py")
            release_module.__package__ = "pisobot"
            exec(compile(source, release_module.__file__, "exec"), release_module.__dict__)
            for attr in ("STATE_DIR", "SEEN_PATH", "passes_basic", "passes_area", "_ensure_photos"):
                setattr(release_module, attr, getattr(main, attr))

        def send(l, reason):
            if users.current_chat() == OWNER and l.key == "rm:2":
                db = sqlite3.connect(localdb.state_dir() / "pisobot.db")
                db.execute("BEGIN IMMEDIATE")
                held.append(db)
                accepted.set()
            return {"result": {"message_id": 1}}

        def verdicts(*a, **kw):
            if kw.get("acknowledged"):
                waiting.set()
            return real_verdicts(*a, **kw)

        def child():
            with patch.object(telegram, "send_listing", side_effect=send), patch.object(archive, "record_verdicts", side_effect=verdicts):
                if release_module is not None:
                    with patch.object(watch, "pipeline", release_module):
                        self.watcher.cycle()
                else:
                    self.watcher.cycle()

        process = ctx.Process(target=child)
        process.start()
        try:
            self.assertTrue(accepted.wait(5), "child never reached fake Telegram acceptance")
            self.assertTrue(waiting.wait(5), "child never reached the contested DB operation")
            time.sleep(0.15)
            self.assertTrue(process.is_alive(), "expected SQLite to be waiting")
            checkpointed = "rm:2" in main.load_seen()
        finally:
            if process.is_alive():
                process.terminate()
            process.join(5)
        self.assertFalse(process.is_alive())
        watch.Watcher(["rm"]).cycle()
        return checkpointed, self.counts()[(OWNER, "rm:2", "new")]

    def test_a_writer_holding_the_database_then_a_kill_does_not_resend(self):
        self.assertEqual(self._contention_crash(), (True, 0))

    def test_the_release_control_behaves_the_same_way(self):
        self.assertEqual(self._contention_crash(release_control=True), (True, 0))

    def test_telemetry_waits_at_most_a_fraction_of_a_second_for_a_contended_writer(self):
        with timing._db():
            pass
        db = sqlite3.connect(localdb.state_dir() / "pisobot.db")
        self.addCleanup(db.close)
        db.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        written = timing.write([timing.capture(home(2), "new", "head")])
        elapsed = time.monotonic() - started
        db.rollback()
        self.assertEqual(written, 0)
        self.assertLess(elapsed, 1.5)
        self.assertGreater(localdb.BUSY_TIMEOUT_MS / 1000, 10)

    def test_a_dead_telemetry_database_does_not_fail_or_lose_an_accepted_card(self):
        self.baseline()
        self.full = [home(1), home(2)]
        with patch.object(timing, "_db", side_effect=sqlite3.OperationalError("fixture telemetry unavailable")):
            self.watcher.cycle()
        self.assertEqual(set(self.counts().values()), {1})
        self.assertIn("rm:2", main.load_seen())

    def test_a_contended_flush_never_holds_up_the_next_cycle(self):
        self.baseline()
        self.full = [home(1), home(2)]
        db = sqlite3.connect(localdb.state_dir() / "pisobot.db")
        self.addCleanup(db.close)
        with timing._db():
            pass
        real_flush = timing.flush
        def flush_under_contention(*a, **kw):
            db.execute("BEGIN IMMEDIATE")
            try:
                return real_flush(*a, **kw)
            finally:
                db.rollback()
        started = time.monotonic()
        with patch.object(timing, "flush", side_effect=flush_under_contention):
            self.watcher.cycle()
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(set(self.counts().values()), {1})


class RetentionTests(TimingBase):
    def test_rows_older_than_thirty_days_are_deleted_on_write_and_by_the_cycle(self):
        now = dt.datetime.now(dt.timezone.utc)
        old = now - dt.timedelta(days=400)
        timing.write([timing.capture(home(i + 10), "new", sent_at=old) for i in range(12)], now=old)
        timing.write([timing.capture(home(2), "new", sent_at=now)], now=now)
        with timing._db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM alert_timing").fetchone()[0], 1)
        timing.write([timing.capture(home(3), "new", sent_at=now - dt.timedelta(days=31))], now=now - dt.timedelta(days=31))
        self.assertEqual(timing.purge_expired(now), 1)

    def test_purge_expired_touches_nothing_when_there_is_no_table(self):
        self.assertEqual(timing.purge_expired(), 0)
        db = Path(localdb.state_dir()) / "pisobot.db"
        if db.exists():
            with sqlite3.connect(db) as conn:
                self.assertFalse(conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'alert_timing'").fetchone())

    def test_a_cycle_runs_the_retention_even_when_it_fails(self):
        with patch.object(main.SOURCES["rm"], "fetch", side_effect=RuntimeError("portal down")), \
                patch.object(timing, "purge_expired") as purge:
            with self.assertRaises(Exception):
                self.watcher.cycle()
        purge.assert_called()


# ---------------------------------------------------------------- what differs from the release, and what does not

def release_module(name, relative):
    source = subprocess.check_output(["git", "-C", str(ROOT), "show", f"origin/release-archive-pipeline:{relative}"],
                                     text=True, stderr=subprocess.DEVNULL)
    module = types.ModuleType(f"pisobot._release_{name}")
    module.__file__ = str(ROOT / relative)
    module.__package__ = "pisobot"
    exec(compile(source, module.__file__, "exec"), module.__dict__)
    return module


def dump(path):
    out = {}
    with contextlib.closing(sqlite3.connect(path)) as db:
        for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"):
            rows = db.execute(f"SELECT * FROM {name}").fetchall()
            out[name] = sorted(re.sub(r"\d{4}-\d\d-\d\dT[\d:.+\-Z]+|[0-9a-f]{16,}", "~", json.dumps(r, default=str)) for r in rows)
    return out


class EquivalenceTests(TimingBase):
    def scenario(self, watcher_class, pipeline_module):
        import tempfile
        state = tempfile.mkdtemp()
        sent, listings = [], [(1, 1200)]
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {"PISOBOT_STATE_DIR": state, "STORAGE": "local"}))
            stack.enter_context(patch.object(main, "STATE_DIR", state))
            stack.enter_context(patch.object(main, "SEEN_PATH", str(Path(state) / "seen.json")))
            stack.enter_context(patch.object(cache, "PATH", str(Path(state) / "cache.json")))
            stack.enter_context(patch.object(cache, "_data", {}))
            stack.enter_context(patch.object(main.SOURCES["rm"], "fetch", side_effect=lambda: [home(i, r) for i, r in listings]))
            stack.enter_context(patch.object(telegram, "send_listing", side_effect=lambda l, r: (
                sent.append((users.current_chat(), l.key, r, telegram.format_listing(l))), {"result": {"message_id": 1}})[1]))
            if pipeline_module is not main:
                for attr in ("STATE_DIR", "SEEN_PATH", "passes_basic", "passes_area", "_ensure_photos"):
                    setattr(pipeline_module, attr, getattr(main, attr))
            stack.enter_context(patch.object(watch, "pipeline", pipeline_module))
            users.register(FRIEND, "Bea", "TheBeatifulest")
            watcher = watcher_class(["rm"])
            for step in ([(1, 1200)], [(1, 1200), (2, 1300)], [(1, 1200), (2, 1250)], [(1, 1200), (2, 1250)]):
                listings[:] = step
                watcher.cycle()
            seen = re.sub(r"\d{4}-\d\d-\d\dT?[\d:.+\-Z]*|[0-9a-f]{16,}", "~", (Path(state) / "seen.json").read_text())
            return sent, seen, dump(Path(state) / "pisobot.db")

    def test_the_only_differences_from_the_release_are_the_timing_table_and_its_rows(self):
        try:
            release_main = release_module("main", "pisobot/main.py")
            release_watch = release_module("watch", "pisobot/watch.py")
        except Exception:
            self.skipTest("release code not available from git")
        release_watch.pipeline = release_main
        theirs = self.scenario(release_watch.Watcher, release_main)
        ours = self.scenario(watch.Watcher, main)
        self.assertEqual(theirs[0], ours[0])                                   # same alerts, same people, same card text
        self.assertEqual(theirs[1], ours[1])                                   # same seen state
        extra = set(ours[2]) - set(theirs[2])
        self.assertEqual(extra, {"alert_timing"})                              # the ONE new table
        self.assertEqual({t: v for t, v in ours[2].items() if t != "alert_timing"}, theirs[2])      # every other table identical
        self.assertEqual(len(ours[2]["alert_timing"]), len([s for s in ours[0]]))                   # one row per accepted alert
        self.assertGreater(len(ours[0]), 2)

    def test_the_search_cache_and_archive_snapshots_are_unchanged(self):
        from pisobot.sources import CACHE_FIELDS, snapshot
        l = home()
        l.exclusive = True
        self.assertNotIn("exclusive", snapshot(l))


if __name__ == "__main__":
    unittest.main()
