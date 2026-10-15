import datetime as dt
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from pisobot import main, prefs


class ScheduleTests(unittest.TestCase):
    def test_missed_morning_slot_catches_up_but_does_not_repeat(self):
        zone = ZoneInfo("Europe/London")
        p = {"last_report": "2026-10-14T21:37"}
        self.assertFalse(main.report_due(p, dt.datetime(2026, 10, 15, 6, 29, tzinfo=zone)))
        self.assertTrue(main.report_due(p, dt.datetime(2026, 10, 15, 6, 30, tzinfo=zone)))
        self.assertTrue(main.report_due(p, dt.datetime(2026, 10, 15, 7, 11, tzinfo=zone)))
        p["last_report"] = "2026-10-15T07:11"
        self.assertFalse(main.report_due(p, dt.datetime(2026, 10, 15, 17, 59, tzinfo=zone)))
        self.assertTrue(main.report_due(p, dt.datetime(2026, 10, 15, 18, 0, tzinfo=zone)))

    def test_slots_follow_london_daylight_saving(self):
        for month, offset in ((1, 0), (7, 1)):
            now = dt.datetime(2026, month, 15, 6, 30, tzinfo=ZoneInfo("Europe/London"))
            self.assertEqual(now.utcoffset().total_seconds(), offset * 3600)
            self.assertTrue(main.report_due({}, now))
            self.assertFalse(main.report_due({"last_report": now.strftime("%Y-%m-%dT%H:%M")}, now))

    def test_every_scheduled_check_can_recover_a_missed_report(self):
        for due in (True, False):
            with patch("sys.argv", ["pisobot", "--scheduled"]), patch.object(prefs, "load", return_value={}), \
                    patch.object(main, "report_due", return_value=due), patch.object(main, "run") as run, \
                    patch("sys.stdout"):
                main.main()
            self.assertEqual(run.called, due)
            if due:
                self.assertTrue(run.call_args.kwargs["scheduled"])
