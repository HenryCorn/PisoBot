"""G1 timing as the first client of the optional foundation (ALERT_TIMING=1), with the reviewer's findings fixed."""
import datetime as dt
import os
import sqlite3
import threading
import time
import unittest
from unittest.mock import patch

from pisobot import optional, status, telegram, timing, users
from pisobot.optional import store
from test_optional import OptionalBase, wait_for
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
        optional.register("ALERT_TIMING", lambda: timing.FEATURE)
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


class OffTests(OptionalBase):
    def test_off_creates_nothing_and_shows_nothing(self):
        self.baseline()
        self.full = [home(1, 1200), home(2, 1250)]
        self.watcher.cycle()
        self.assertFalse(store.exists())
        self.assertEqual(timing.status_lines(), [])
        with sqlite3.connect(users.localdb.state_dir() / "pisobot.db") as db:
            self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE name = 'alert_timing'").fetchall(), [])



if __name__ == "__main__":
    unittest.main()
