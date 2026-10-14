import io
import os
import re
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from pisobot import collage, main, prefs, telegram
from pisobot.models import Listing


def home(i, **changes):
    values = dict(source="stb", id=str(i), url=f"https://example.com/home/{i}",
                  address=f"Home {i} & <Court>", bedrooms=2, scheme="Shared Ownership",
                  monthly_cost=1000 + i, deposit=10000, share_price=100000,
                  travel_minutes=20, crime_score=30, lat=51.5, lng=-0.1)
    values.update(changes)
    return Listing(**values)


class ReportTests(unittest.TestCase):
    def test_schedule_always_requests_reports_in_uk_time(self):
        workflow = (Path(__file__).resolve().parents[1] / ".github" / "workflows" / "pisobot.yml").read_text()
        schedules = re.findall(r'cron: "([^"]+)"\s+timezone: "([^"]+)"', workflow)
        self.assertEqual(schedules, [("30 6 * * *", "Europe/London"),
                                     ("0 18 * * *", "Europe/London")])
        self.assertIn("github.event_name == 'schedule'", workflow)

    def test_cards_and_buttons_stay_together_without_duplicate_saved_homes(self):
        top = [home(i) for i in range(10)]
        saved = [top[0], home(20)]
        rest = [home(i) for i in range(30, 130)]
        messages = telegram.report_messages(top, rest, saved, "Report", lambda l: "")
        text = "\n".join(t for t, _ in messages)
        self.assertEqual(text.count("Home 0 &amp; &lt;Court&gt;"), 1)
        self.assertIn("100 homes", text)
        self.assertIn("Compare all on map", text)
        self.assertIn("More saved homes (1)", text)
        for t, keyboard in messages:
            self.assertLessEqual(len(t.encode("utf-16-le")) // 2, 4096)
            for tag in ("b", "i", "a"):
                self.assertEqual(len(re.findall(fr"<{tag}(?:>| )", t)), t.count(f"</{tag}>"))
            if keyboard:
                for row in keyboard["inline_keyboard"]:
                    rank = int(row[0]["text"].split("#")[1])
                    self.assertIn(f"<b>#{rank}</b>", t)
                    self.assertEqual(row[0]["url"], top[rank - 1].url)
                    self.assertEqual(row[1]["callback_data"], f"s:{top[rank - 1].short_id}")
                    self.assertEqual(row[2]["callback_data"], f"h:{top[rank - 1].short_id}")

    def test_rent_estimates_and_empty_report(self):
        rented = home(1, tenure="rent", deposit=None, share_price=None, estimated=["rent"])
        text = "\n".join(t for t, _ in telegram.report_messages([rented], [], [], "Report", lambda l: ""))
        self.assertIn("no mortgage needed", text)
        self.assertIn("(est.)", text)
        empty = telegram.report_messages([], [], [], "Report", lambda l: "")
        self.assertEqual(len(empty), 1)
        self.assertIn("No homes matched", empty[0][0])

    def test_alternatives_are_bounded_and_cheapest_first(self):
        rest = [home(i, lat=51.5 + i * 0.01) for i in reversed(range(100))]
        messages = telegram.report_messages([], rest, [], "Report", lambda l: "")
        self.assertEqual(len(messages), 2)
        text = messages[1][0]
        self.assertIn("100 homes", text)
        self.assertIn("Showing the 5 cheapest places", text)
        self.assertIn("Home 0 &amp;", text)
        self.assertIn("Home 4 &amp;", text)
        self.assertNotIn("Home 5 &amp;", text)
        self.assertIn("Browse all 100", text)

    def test_saving_does_not_remove_other_report_buttons(self):
        a, b = home(1), home(2)
        known = {l.key: {"lat": l.lat, "lng": l.lng, "url": l.url, "address": l.address,
                         "cost": l.monthly_cost} for l in (a, b)}
        report_keyboard = telegram.report_messages([a, b], [], [], "Report", lambda l: "")[1][1]
        for keyboard, should_mark in ((report_keyboard, False), (telegram.listing_keyboard(a), True)):
            p = {"offset": 0, "saved": {}, "hidden": [], "hidden_dev": []}
            update = {"update_id": 1, "callback_query": {
                "id": "callback", "data": f"s:{a.short_id}",
                "message": {"chat": {"id": 123}, "message_id": 5, "reply_markup": keyboard}}}
            with patch.dict(os.environ, TELEGRAM_CHAT_ID="123"), \
                    patch.object(telegram, "get_updates", return_value=[update]), \
                    patch.object(telegram, "answer_callback"), patch.object(telegram, "mark_message") as mark, \
                    patch("sys.stdout", new=io.StringIO()):
                prefs.process_updates(p, known)
                self.assertIn(a.key, p["saved"])
                self.assertEqual(mark.called, should_mark)

    def test_full_report_run_sends_digest_and_records_reported_homes(self):
        a, b = home(1), home(2, lat=51.52)
        seen = {a.key: {"alerted": True, "cost": 1001, "first": "2026-01-01T06:30"}}
        p = {"saved": {}, "hidden": [], "hidden_dev": [], "report_requested": False,
             "last_report": "2026-01-01T06:30"}
        with patch.object(main, "load_seen", return_value=seen), \
                patch.object(prefs, "load", return_value=p), patch.object(prefs, "process_updates"), \
                patch.object(main.SOURCES["stb"], "fetch", return_value=[a, b]), \
                patch.object(main.enrich, "geocode"), patch.object(main, "passes_basic", return_value=None), \
                patch.object(main, "passes_area", return_value=None), patch.object(main.mapgen, "write"), \
                patch.object(collage, "build", return_value=None), patch.object(telegram, "throttle"), \
                patch.object(telegram, "send_text") as send, patch.object(telegram, "send_listing") as alert, \
                patch.object(main.cache, "save"), patch.object(main, "save_seen") as save_seen, \
                patch.object(prefs, "save"), patch("sys.stdout", new=io.StringIO()):
            main.run(["stb"], False, False, report=True)
        self.assertIn("2 homes", send.call_args_list[0].args[0])
        self.assertIn("1 new", send.call_args_list[0].args[0])
        self.assertEqual(send.call_count, 2)
        alert.assert_not_called()
        save_seen.assert_called_once_with(seen)
        self.assertTrue(seen[b.key]["alerted"])
        self.assertGreater(p["last_report"], "2026-01-01T06:30")

    def test_mobile_collage_handles_missing_photos(self):
        thumbnail = Image.new("RGB", (collage.TILE_W, collage.TILE_H), "navy")
        with patch.object(collage, "_thumb", side_effect=[thumbnail, None, thumbnail]):
            jpeg = collage.build([home(1), home(2), home(3)])
        with Image.open(io.BytesIO(jpeg)) as image:
            self.assertEqual(image.size, (840, 696))
            self.assertEqual(image.format, "JPEG")
        with patch.object(collage, "_thumb", return_value=None):
            self.assertIsNone(collage.build([home(1)]))
            self.assertIsNone(collage.build([]))


if __name__ == "__main__":
    unittest.main()
