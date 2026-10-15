import io
import json
import re
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from pisobot import collage, d1, main, prefs, telegram
from pisobot.models import Listing
from pisobot.sources import onthemarket, sharetobuy


def home(i, **changes):
    values = dict(source="stb", id=str(i), url=f"https://example.com/home/{i}",
                  address=f"Home {i} & <Court>", bedrooms=2, scheme="Shared Ownership",
                  monthly_cost=1000 + i, deposit=10000, share_price=100000,
                  travel_minutes=20, crime_score=30, lat=51.5, lng=-0.1,
                  image=f"https://example.com/{i}/1.jpg",
                  images=[f"https://example.com/{i}/{n}.jpg" for n in range(1, 4)])
    values.update(changes)
    return Listing(**values)


class ReportTests(unittest.TestCase):
    def test_schedule_always_requests_reports_in_uk_time(self):
        workflow = (Path(__file__).resolve().parents[1] / ".github" / "workflows" / "pisobot.yml").read_text()
        schedules = re.findall(r'cron: "([^"]+)"\s+timezone: "([^"]+)"', workflow)
        self.assertEqual(schedules, [("40 * * * *", "Europe/London")])  # catch-up backstop
        self.assertIn("SCHEDULED: ${{ github.event_name == 'schedule' || inputs.scheduled }}", workflow)
        self.assertIn("--scheduled", workflow)
        self.assertIn("CLOUDFLARE_D1_DATABASE_ID", workflow)
        self.assertIn("ref: ${{ github.ref }}", workflow)

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
                rank = int(re.search(r"<b>#(\d+) ·", t).group(1))
                self.assertEqual(keyboard, telegram.listing_keyboard(top[rank - 1], saved=top[rank - 1] in saved))
                self.assertLessEqual(len(t.encode("utf-16-le")) // 2, 1024)

    def test_rent_estimates_and_empty_report(self):
        rented = home(1, tenure="rent", deposit=None, share_price=None, estimated=["rent"])
        text = "\n".join(t for t, _ in telegram.report_messages([rented], [], [], "Report", lambda l: ""))
        self.assertIn("no mortgage needed", text)
        self.assertIn("est. rent", text)
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

    def test_prefs_round_trip_through_d1_without_overwriting_worker_taps(self):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.executescript((Path(__file__).resolve().parents[1] / "worker" / "schema.sql").read_text())

        def query(sql, *params):
            rows = [dict(row) for row in db.execute(sql, params)]
            db.commit()
            return rows

        a, b = home(1), home(2)
        known = {l.key: {"lat": l.lat, "lng": l.lng, "url": l.url, "address": l.address,
                         "cost": round(l.monthly_cost)} for l in (a, b)}
        with patch.object(d1, "configured", return_value=True), patch.object(d1, "query", side_effect=query):
            prefs.publish(known, chunk=1)
            # card buttons carry Listing.short_id; the Worker looks that up in `listings`
            self.assertEqual(query("SELECT key FROM listings WHERE sid = ?", a.short_id), [{"key": a.key}])
            query("INSERT INTO saved VALUES (?, ?)", a.key, json.dumps({**known[a.key], "missing": 0}))
            query("INSERT INTO hidden_dev VALUES (?, ?)", b.lat, b.lng)
            p = prefs.load()
            self.assertEqual(set(p["saved"]), {a.key})
            self.assertTrue(prefs.is_hidden(p, b))
            query("INSERT INTO saved VALUES (?, ?)", b.key, "{}")  # tapped while the run is busy
            p["saved"][a.key]["cost"] = 990
            p["last_report"] = "2026-01-02T18:00"
            prefs.save(p)
            self.assertEqual(json.loads(query("SELECT info FROM saved WHERE key = ?", a.key)[0]["info"])["cost"], 990)
            self.assertEqual(len(query("SELECT key FROM saved WHERE key = ?", b.key)), 1)
            self.assertEqual(prefs.load()["last_report"], "2026-01-02T18:00")
            del p["saved"][a.key]  # a saved home that disappeared from the sources
            prefs.save(p)
            self.assertEqual([r["key"] for r in query("SELECT key FROM saved")], [b.key])

    def test_full_report_run_sends_digest_and_records_reported_homes(self):
        a, b = home(1), home(2, lat=51.52)
        seen = {a.key: {"alerted": True, "cost": 1001, "first": "2026-01-01T06:30"}}
        p = {"saved": {}, "hidden": [], "hidden_dev": [], "last_report": "2026-01-01T06:30"}
        with patch.object(main, "load_seen", return_value=seen), \
                patch.object(prefs, "load", return_value=p), patch.object(prefs, "publish"), \
                patch.object(main.SOURCES["stb"], "fetch", return_value=[a, b]), \
                patch.object(main.enrich, "geocode"), patch.object(main, "passes_basic", return_value=None), \
                patch.object(main, "passes_area", return_value=None), \
                patch.object(main.enrich, "assign_crime_percentiles"), patch.object(main.mapgen, "write"), \
                patch.object(collage, "build", return_value=None), patch.object(telegram, "throttle"), \
                patch.object(telegram, "send_text") as send, patch.object(telegram, "send_listing") as alert, \
                patch.object(main.cache, "save"), patch.object(main, "save_seen") as save_seen, \
                patch.object(prefs, "save"), patch("sys.stdout", new=io.StringIO()):
            main.run(["stb"], False, False, report=True)
        self.assertIn("2 homes", send.call_args_list[0].args[0])
        self.assertIn("1 new", send.call_args_list[0].args[0])
        self.assertEqual(send.call_count, 3)
        alert.assert_not_called()
        save_seen.assert_called_once_with(seen)
        self.assertTrue(seen[b.key]["alerted"])
        self.assertGreater(p["last_report"], "2026-01-01T06:30")

    def test_header_prefers_floorplan_and_can_send_map_without_photos(self):
        photo = Image.new("RGB", (720, 500), "navy")
        location = Image.new("RGB", (480, 500), "grey")
        l = home(1, floorplan="https://example.com/plan.png")
        with patch.object(collage, "_thumb", return_value=photo) as thumb, \
                patch.object(collage, "_map", return_value=location):
            jpeg = collage.build(l)
        thumb.assert_called_once_with(l.floorplan, floorplan=True)
        with Image.open(io.BytesIO(jpeg)) as image:
            self.assertEqual(image.size, (1200, 500))
            self.assertEqual(image.format, "JPEG")
            self.assertGreater(image.getpixel((10, 100))[0], 100)
            self.assertLess(image.getpixel((850, 100))[0], 10)
        with patch.object(collage, "_thumb", return_value=None), patch.object(collage, "_map", return_value=None):
            self.assertIsNone(collage.build(l))
            self.assertIsNone(collage.build(home(1, image=None, images=[])))
        with patch.object(collage, "_thumb", return_value=None), patch.object(collage, "_map", return_value=location):
            self.assertIsNotNone(collage.build(l))

    def test_property_gallery_excludes_other_listings(self):
        urls = [f"https://sih.cdn.sharetobuy.com/photo{i}" for i in range(3)]
        html = ('<section id="property-1"><div class="card-images-main-image">'
                + "".join(f'<img data-src-original="{url}">' for url in urls + urls)
                + '<div class="card-images-secondary-image"></div></section>'
                + '<img data-src-original="https://sih.cdn.sharetobuy.com/unrelated">')
        self.assertEqual(sharetobuy._photo_urls(html, "1"), urls)
        with self.assertRaises(ValueError):
            sharetobuy._photo_urls(html, "2")

    def test_onthemarket_gallery_prefers_full_photos_and_skips_floorplans(self):
        property = {"images": [{"largeUrl": "https://example.com/room.jpg", "url": "tiny.jpg"},
                               {"largeUrl": "https://example.com/room.jpg"},
                               {"largeUrl": "https://example.com/plan.jpg", "isImage": False},
                               {"url": "https://example.com/kitchen.jpg"}]}
        self.assertEqual(onthemarket._photo_urls(property),
                         ["https://example.com/room.jpg", "https://example.com/kitchen.jpg"])

    def test_report_attaches_thumbnails_and_controls_to_each_card(self):
        a, b = home(1), home(2)
        with patch.object(collage, "build", return_value=b"jpeg") as build, \
                patch.object(telegram, "send_photo_bytes") as photo, \
                patch.object(telegram, "send_text") as text, patch.object(telegram, "throttle"), \
                patch("sys.stdout", new=io.StringIO()):
            main.send_report([a, b], [], [], "Report", lambda l: "")
        self.assertEqual(build.call_count, 2)
        self.assertEqual(photo.call_count, 2)
        self.assertEqual(text.call_count, 1)
        for call, l in zip(photo.call_args_list, (a, b)):
            self.assertIn(l.address.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"), call.args[1])
            self.assertEqual(call.args[2], telegram.listing_keyboard(l))

    def test_long_caption_keeps_details_and_buttons_in_following_message(self):
        l = home(1, address="<" * 90, route="&" * 160)
        with patch.object(collage, "build", return_value=b"jpeg"), \
                patch.object(telegram, "send_photo_bytes") as photo, \
                patch.object(telegram, "send_text") as text, patch.object(telegram, "throttle"), \
                patch("sys.stdout", new=io.StringIO()):
            main.send_report([l], [], [], "Report", lambda l: "")
        self.assertEqual(photo.call_count, 1)
        self.assertLessEqual(len(photo.call_args.args[1]), 1024)
        self.assertEqual(text.call_args.args[1], telegram.listing_keyboard(l))
        self.assertIn("&lt;" * 90, text.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
