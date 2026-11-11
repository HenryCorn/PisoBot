"""G1 timing as the first client of the optional foundation (ALERT_TIMING=1), with the reviewer's findings fixed."""
import datetime as dt
import os
import sqlite3
import threading
import time
import unittest
from unittest.mock import patch

from pisobot import optional, registry_loader, status, telegram, timing, users
from pisobot.optional import store
from pisobot.sources import rightmove
import test_optional
from test_optional import OptionalBase, REAL_SEND_LISTING, REAL_SEND_TEXT, wait_for
from test_portal_facts import rm_property
from test_users import OWNER, home

NOW = dt.datetime.now(dt.timezone.utc)


def iso(minutes_ago):
    return (NOW - dt.timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")


class TimingBase(OptionalBase):
    def setUp(self):
        super().setUp()
        timing.stats.update(retried=0, written=0)
        self.baseline()
        optional.reset()
        optional.register("ALERT_TIMING", lambda: timing.FEATURE, templates=registry_loader.TIMING_TEMPLATES)
        patcher = patch.dict(os.environ, {"ALERT_TIMING": "1"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def rows(self):
        return store.read("SELECT * FROM alert_timing ORDER BY id")

    def settle(self, count=None):
        """Wait for the feature's worker to have written (or kept) everything handed to it."""
        wait_for(lambda: all(w["waiting"] == 0 and not w["stuck"] for w in optional.health()["workers"]) and
                 (count is None or len(self.rows()) >= count))


class StatisticsTests(unittest.TestCase):
    def test_the_median_is_the_conventional_one(self):
        self.assertEqual(timing.median([10, 20]), 15)
        self.assertEqual(timing.median([10, 20, 30, 40]), 25)
        self.assertEqual(timing.median([30, 10, 20]), 20)
        self.assertIsNone(timing.median([]))

    def test_the_90th_percentile_needs_enough_alerts(self):
        with patch.object(timing.store, "read", return_value=[
                {"source": "rm", "via": "head", "listed_at": iso(60 + i), "first_fetch_at": iso(50), "sent_at": iso(i)} for i in range(4)]):
            row = timing.report(now=NOW)["rm"]
        self.assertEqual((row["portal_median"], row["portal_p90"]), (60, None))


class RecordingTests(TimingBase):
    def test_one_row_per_accepted_alert_with_the_archives_first_sight(self):
        self.full = [home(1, 1200), home(2, 1250)]
        self.watcher.cycle()
        self.settle(len(self.sent))
        rows = self.rows()
        self.assertEqual(len(rows), len(self.sent))
        self.assertTrue(all(r["via"] == "head" and r["reason"] == "new" for r in rows))
        self.assertTrue(any(r["first_fetch_at"] for r in rows))

    def test_a_partially_failed_cycle_keeps_the_alerts_that_were_accepted(self):
        real = telegram.send_listing

        def flaky(l, reason):
            if l.key == "rm:2" and users.current_chat() == OWNER:
                raise RuntimeError("telegram down")
            return real(l, reason)
        self.full = [home(1, 1200), home(2, 1250)]
        with patch.object(telegram, "send_listing", side_effect=flaky), self.assertRaises(Exception):
            self.watcher.cycle()
        self.settle(1)
        self.assertGreater(len(self.rows()), 0)

    def test_a_contended_database_retries_without_loss_or_duplicates_and_says_so(self):
        store.write(timing.DDL, [])
        holder = sqlite3.connect(store.path())
        holder.execute("BEGIN IMMEDIATE")
        self.addCleanup(holder.close)
        self.full = [home(1, 1200), home(2, 1250)]
        self.watcher.cycle()
        sent = len(self.sent)
        self.assertTrue(wait_for(lambda: timing.stats["retried"] >= sent))
        holder.rollback()
        self.watcher.cycle()                      # nothing new to alert; the worker also retries what was kept
        self.assertTrue(wait_for(lambda: len(self.rows()) == sent, 10))
        time.sleep(0.2)
        self.assertEqual(len(self.rows()), sent)  # no duplicates
        self.assertIn("retried since start", "\n".join(timing.status_lines()))

    def test_dropped_rows_are_visible(self):
        optional.lost["evicted"] = 4
        self.assertIn("evicted 4", "\n".join(timing.status_lines()))

    def test_old_rows_are_deleted_by_a_write(self):
        old = (NOW - dt.timedelta(days=40)).isoformat(timespec="seconds")
        store.write(timing.DDL, [("INSERT INTO alert_timing (source, reason, via, sent_at) VALUES ('rm','new','head',?)", (old,))])
        self.full = [home(1, 1200), home(2, 1250)]
        self.watcher.cycle()
        self.settle(2)
        self.assertTrue(self.rows())
        self.assertTrue(all(r["sent_at"] > old for r in self.rows()))

    def test_the_path_comes_from_the_hook_context(self):
        optional.after_ack(home(1), OWNER, None, {"reason": "new", "state": "sent", "via": "sweep"})
        self.assertEqual(timing.row_for(optional._items[-1])["via"], "sweep")
        optional.after_ack(home(2), OWNER, None, {"reason": "new", "state": "told"})
        self.assertIsNone(timing.row_for(optional._items[-1]))

    def test_the_feature_runs_on_its_own_thread_never_the_pipeline(self):
        threads = set()
        real = timing.Feature.after_cycle
        with patch.object(timing.Feature, "after_cycle", lambda self, items: (threads.add(threading.current_thread()), real(self, items))[1]):
            self.full = [home(1, 1200), home(2, 1250)]
            self.watcher.cycle()
            self.assertTrue(wait_for(lambda: threads))
        self.assertNotIn(threading.current_thread(), threads)


class StatusTests(TimingBase):
    def test_status_never_creates_the_file_or_the_table(self):
        self.assertEqual(timing.status_lines(), [])
        status.status_text()
        self.assertFalse(store.exists())

    def test_status_shows_the_delay_once_alerts_are_recorded(self):
        l = home(1)
        l.first_listed_at = iso(30)
        optional.after_ack(l, OWNER, None, {"reason": "new", "state": "sent"})
        optional.after_cycle()
        self.settle(1)
        text = "\n".join(timing.status_lines())
        self.assertIn("Alert delay", text)
        self.assertIn("Rightmove: 1 alerts", text)

    def test_an_alert_whose_write_is_in_flight_is_shown_not_hidden(self):
        """Re-review P3: the batch has left the inbox and the write has not settled; /status must still say so."""
        entered, release, real = threading.Event(), threading.Event(), store.write
        self.addCleanup(release.set)

        def slow(*a, **kw):
            entered.set()
            release.wait(5)
            return real(*a, **kw)
        with patch.object(store, "write", side_effect=slow):
            optional.after_ack(home(1), OWNER, None, {"reason": "new", "state": "sent"})
            optional.after_cycle()
            self.assertTrue(entered.wait(5))
            text = status.status_text()
            self.assertIn("Alert delay", text)
            self.assertIn("1 being written now", text)
            release.set()
            self.settle(1)
        self.assertTrue(wait_for(lambda: "being written now" not in "\n".join(timing.status_lines())))


class ListedAgoTests(TimingBase):
    """The "Listed N min ago" line goes through the foundation's fetch-to-send seam: declared once, rendered by the foundation."""

    def card(self, listing, photo=True):
        calls = []
        with patch.object(telegram, "listing_keyboard", return_value={}), patch.object(telegram, "remember_photos"), \
                patch.object(telegram.collage, "build", return_value=b"jpeg" if photo else None), \
                patch.object(telegram, "send_text", REAL_SEND_TEXT), \
                patch.object(telegram, "_call", side_effect=lambda m, **kw: (calls.append((m, kw)), {})[1]):
            REAL_SEND_LISTING(listing)
        return calls

    def text(self, calls):
        return "".join(str(kw.get("caption") or kw.get("text") or "") for _, kw in calls)

    def declared(self):
        self.assertTrue(optional._templates)
        self.assertEqual(optional.workers(), [])

    def test_the_first_alert_of_a_fresh_advert_says_how_long_ago_it_was_listed(self):
        self.declared()
        for photo in (True, False):
            fresh = home(2, 1300)
            fresh.first_listed_at = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=40, seconds=20)).isoformat()
            calls = self.card(fresh, photo)
            self.assertIn("\n🕒 Listed 40 min ago", self.text(calls), (photo, calls))

    def test_only_under_three_hours_and_only_with_a_real_timestamp(self):
        self.declared()
        for minutes, shown in ((179, "Listed 2 h 59 min ago"), (180, None), (181, None), (60 * 30, None), (-30, None)):
            l = home(2, 1300)
            l.first_listed_at = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=minutes, seconds=20)).isoformat()
            text = self.text(self.card(l))
            self.assertEqual("Listed" in text, shown is not None, minutes)
            if shown:
                self.assertIn(shown, text)
        l = home(2, 1300)
        self.assertNotIn("Listed", self.text(self.card(l)))                   # the portal gave no timestamp
        l.first_listed_at = "2026-11-11"
        self.assertNotIn("Listed", self.text(self.card(l)))                   # a bare date is not a listing time

    def test_the_line_never_changes_the_number_of_telegram_calls(self):
        l = home(2, 1300)
        l.first_listed_at = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=40, seconds=20)).isoformat()
        l.station = "S" * 3000
        with patch.dict(os.environ, {"ALERT_TIMING": "0"}):
            before = [m for m, _ in self.card(l)]
        self.declared()
        self.assertEqual([m for m, _ in self.card(l)], before)

    def test_the_feature_declares_data_and_none_of_its_code_runs_when_a_card_is_built(self):
        self.declared()
        self.assertEqual([(t.field, t.kind, t.max_seconds) for t in optional._templates], [("first_listed_at", "age", 3 * 3600)])
        fresh = home(2, 1300)
        fresh.first_listed_at = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5, seconds=20)).isoformat()
        tripwire = AssertionError("timing code ran on the delivery thread")
        with patch.object(timing, "row_for", side_effect=tripwire), patch.object(timing, "write_rows", side_effect=tripwire), \
                patch.object(timing.Feature, "after_cycle", side_effect=tripwire), \
                patch.object(store, "write", side_effect=tripwire), patch.object(store, "read", side_effect=tripwire), \
                patch.object(store, "read_primary", side_effect=tripwire):
            self.assertIn("Listed 5 min ago", self.text(self.card(fresh)))
        self.assertEqual(optional.workers(), [])

    def test_a_real_search_card_timestamp_reaches_the_first_alert_without_starting_a_worker(self):
        self.declared()
        listed = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=40, seconds=20)).isoformat()
        listing = rightmove._listing(rm_property(2, firstVisibleDate=listed))
        listing.monthly_cost, listing.deposit, listing.travel_minutes, listing.crime_score = 1580, 1500, 25, 30
        self.assertIn("Listed 40 min ago", self.text(self.card(listing)))
        self.assertEqual(optional.workers(), [])

    def test_the_shipped_registration_declares_the_template_before_loading_the_feature(self):
        import importlib
        optional.reset()
        with patch.object(registry_loader.importlib, "import_module", side_effect=AssertionError("feature imported")):
            importlib.reload(registry_loader)
            self.assertEqual(len(optional._templates), 1)
            fresh = home(2, 1300)
            fresh.first_listed_at = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=40, seconds=20)).isoformat()
            self.assertIn("Listed 40 min ago", self.text(self.card(fresh)))
        flag, loader = optional._registry[-1]
        self.assertEqual(flag, "ALERT_TIMING")
        self.assertIs(loader(), timing.FEATURE)


class OffTests(OptionalBase):
    def test_off_creates_nothing_and_shows_nothing(self):
        import importlib
        with patch.dict(os.environ, {"ALERT_TIMING": "0"}):
            importlib.reload(registry_loader)
            self.baseline()
            self.full = [home(1, 1200), home(2, 1250)]
            self.watcher.cycle()
            self.assertFalse(store.exists())
            self.assertEqual(timing.status_lines(), [])
            self.assertEqual((len(optional._templates), optional.workers()), (1, []))
            fresh = home(3, 1100)
            fresh.first_listed_at = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=40)).isoformat()
            self.assertEqual(optional.extend_card("card", fresh, True), "card")
            with sqlite3.connect(users.localdb.state_dir() / "pisobot.db") as db:
                self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE name = 'alert_timing'").fetchall(), [])


class TimingOffTraceTests(test_optional.RealSendBase):
    scenario = test_optional.OffIsTheReleaseTests.scenario

    def setUp(self):
        super().setUp()
        optional.reset()
        self.addCleanup(optional.reset)

    def test_the_shipped_feature_disabled_has_the_releases_complete_trace(self):
        import importlib
        with patch.dict(os.environ, {"ALERT_TIMING": "0"}):
            importlib.reload(registry_loader)
            theirs, ours = self.scenario(True), self.scenario(False)
        self.assertGreater(len([event for event in ours[0] if event[0] == "telegram"]), 3)
        self.assertEqual(theirs, ours)
        self.assertEqual(optional.workers(), [])



if __name__ == "__main__":
    unittest.main()
