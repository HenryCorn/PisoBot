import copy
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from pisobot import cache, config, d1, main, telegram, watch
from pisobot.models import Listing


def home(i=1, cost=1500):
    return Listing("rm", str(i), f"https://example.com/{i}", f"Home {i}", 1, "Private rent",
                   tenure="rent", rent=cost - 280, monthly_cost=cost, deposit=1500,
                   lat=51.50 + i * 0.02, lng=-0.18, travel_minutes=25, crime_score=30)


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        for target in (
            patch.dict(os.environ, {"STORAGE": "local", "PISOBOT_STATE_DIR": self.tmp.name,
                                    "HEALTHCHECK_URL": ""}),
            patch.object(main, "STATE_DIR", self.tmp.name),
            patch.object(main, "SEEN_PATH", str(self.state / "seen.json")),
            patch.object(cache, "PATH", str(self.state / "cache.json")),
            patch.object(cache, "_data", {}),
            patch.object(main.enrich, "geocode"),
            patch.object(main.enrich, "assign_crime_percentiles"),
            patch.object(main, "passes_basic", return_value=None),
            patch.object(main, "passes_area", return_value=None),
            patch.object(main, "_ensure_photos"),
            patch.object(telegram, "throttle"),
            patch("sys.stdout"),
            patch("sys.stderr"),
        ):
            target.start()
            self.addCleanup(target.stop)
        self.fetch = self.enterContext(patch.object(main.SOURCES["rm"], "fetch", return_value=[home()]))
        self.send = self.enterContext(patch.object(telegram, "send_listing"))
        self.text = self.enterContext(patch.object(telegram, "send_text"))
        self.watcher = watch.Watcher(["rm"])

    def test_baseline_then_new_and_drop_survive_restart_without_duplicates(self):
        self.watcher.cycle()
        self.send.assert_not_called()
        self.text.assert_called_once()
        self.assertIn("1 current matches", self.text.call_args.args[0])
        self.assertEqual(watch._meta("watch_initialized"), "1")
        self.fetch.return_value = [home(cost=1450), home(2)]
        watch.Watcher(["rm"]).cycle()
        self.assertEqual({(call.args[0].key, call.args[1]) for call in self.send.call_args_list},
                         {("rm:1", "drop"), ("rm:2", "new")})
        self.send.reset_mock()
        watch.Watcher(["rm"]).cycle()
        self.send.assert_not_called()
        self.text.assert_called_once()
        self.assertEqual(main.load_seen()["rm:1"]["cost"], 1450)

    def test_failed_initial_enrichment_does_not_initialize_or_send(self):
        with patch.object(main, "passes_area", side_effect=RuntimeError("unavailable")):
            with self.assertRaisesRegex(RuntimeError, "baseline incomplete"):
                self.watcher.cycle()
        self.assertIsNone(watch._meta("watch_initialized"))
        self.assertFalse((self.state / "seen.json").exists())
        self.send.assert_not_called()
        self.text.assert_not_called()
        self.watcher.cycle()
        self.text.assert_called_once()

    def test_empty_successful_baseline_is_not_repeated(self):
        self.fetch.return_value = []
        self.watcher.cycle()
        self.watcher.cycle()
        self.text.assert_called_once()
        self.assertEqual(main.load_seen(), {})

    def test_dry_run_does_not_seed_or_send_and_does_not_touch_webhook(self):
        self.watcher.cycle(dry_run=True)
        self.send.assert_not_called()
        self.text.assert_not_called()
        self.assertIsNone(watch._meta("watch_initialized"))
        self.assertFalse((self.state / "seen.json").exists())

    def test_saved_price_increase_does_not_alert_but_drop_does(self):
        self.watcher.cycle()
        d1.query("INSERT INTO saved (key, info) VALUES (?, ?)", "rm:1",
                 json.dumps({"cost": 1500, "missing": 0}))
        self.fetch.return_value = [home(cost=1550)]
        self.watcher.cycle()
        self.send.assert_not_called()
        self.fetch.return_value = [home(cost=1450)]
        self.watcher.cycle()
        self.assertEqual(self.send.call_args.args[1], "drop")

    def test_capped_scan_does_not_remove_saved_home(self):
        self.watcher.cycle()
        d1.query("INSERT INTO saved (key, info) VALUES (?, ?)", "rm:missing",
                 json.dumps({"cost": 1300, "missing": 1}))
        self.watcher.cycle()
        self.watcher.cycle()
        self.assertTrue(d1.query("SELECT key FROM saved WHERE key = ?", "rm:missing"))
        self.text.assert_called_once()

    def test_unsent_matches_remain_pending_after_alert_cap(self):
        self.watcher.cycle()
        self.fetch.return_value = [home(), home(2), home(3)]
        with patch.object(config, "MAX_ALERTS_PER_RUN", 1):
            self.watcher.cycle()
            self.watcher.cycle()
        self.assertEqual([call.args[0].key for call in self.send.call_args_list], ["rm:2", "rm:3"])

    def test_each_delivered_alert_is_checkpointed_before_next_send(self):
        self.watcher.cycle()
        self.fetch.return_value = [home(), home(2), home(3)]
        self.send.side_effect = [None, RuntimeError("delivery failed")]
        with self.assertRaisesRegex(RuntimeError, "delivery failed"):
            self.watcher.cycle()
        self.assertIn("rm:2", main.load_seen())
        self.assertNotIn("rm:3", main.load_seen())
        self.send.reset_mock(side_effect=True)
        self.watcher.cycle()
        self.assertEqual([call.args[0].key for call in self.send.call_args_list], ["rm:3"])

    def test_run_requests_are_coalesced_and_invalid_commands_rejected(self):
        self.assertTrue(self.watcher.request_run("rentals"))
        self.assertFalse(self.watcher.request_run("report"))
        self.assertEqual(self.watcher.pending.get_nowait(), "rentals")
        self.watcher.running = True
        self.assertFalse(self.watcher.request_run("rentals"))
        with self.assertRaises(ValueError):
            self.watcher.request_run("shell")

    def test_warning_sent_once_across_restarts_at_failure_threshold(self):
        with patch.object(config, "WATCH_FAILURE_LIMIT", 2):
            self.watcher._failed()
            self.text.assert_not_called()
            self.watcher._failed()
            watch.Watcher(["rm"])._failed()
        self.text.assert_called_once()
        self.assertEqual(watch._meta("watch_failures"), "3")

    def test_process_lock_rejects_second_runner(self):
        with main.run_lock():
            with self.assertRaisesRegex(RuntimeError, "already running"):
                main.run(["rm"], True, False, watch=True)

    def test_stop_before_cycle_has_no_side_effects(self):
        stop = threading.Event()
        stop.set()
        with self.assertRaises(InterruptedError):
            self.watcher.cycle(stop_event=stop)
        self.fetch.assert_not_called()
        self.text.assert_not_called()

    def test_atomic_seen_failure_preserves_previous_state(self):
        main.save_seen({"old": {"cost": 1500}})
        with patch.object(main.os, "replace", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                main.save_seen({"new": {}})
        self.assertEqual(main.load_seen(), {"old": {"cost": 1500}})
        self.assertEqual([p.name for p in self.state.iterdir()], ["seen.json"])

    def test_corrupt_local_seen_is_not_silently_discarded(self):
        (self.state / "seen.json").write_text("{broken")
        with self.assertRaises(json.JSONDecodeError):
            main.load_seen()

    def test_local_report_omits_stale_cloud_map_link(self):
        with patch.object(config, "MAP_URL", ""):
            messages = telegram.report_messages([home()], [], [], "Rentals", lambda _: "")
        self.assertNotIn("Browse photos", messages[0][0])


if __name__ == "__main__":
    unittest.main()
