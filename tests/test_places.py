import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from pisobot import main, places, telegram, tgtext
from tests.test_card_ui import home


def fixture(path, points):
    Path(path).write_text(json.dumps({"points": points}), encoding="utf-8")


POINTS = {
    "supermarket": [[51.5000, 0.0200, "Tesco"], [51.6000, 0.0200, "Far Away"]],
    "convenience": [[51.5009, 0.0200, "Co-op <b>&"]],
    "gym": [[51.5030, 0.0200, "PureGym With A Very Long Name"]],
    "cinema": [[51.5100, 0.0200, "Odeon"], [51.5, 0.2, "Too Far"]],
}


class PlaceCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "tiles.json"
        fixture(self.path, POINTS)
        self.patches = [patch.object(places, "DATA", self.path), patch.dict(places._state, {"loaded": False, "grid": None})]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()


class LookupTests(PlaceCase):
    def test_nearest_distance_and_minutes(self):
        d, name = places.nearest(51.5, 0.02, "supermarket")
        self.assertEqual((round(d), name), (0, "Tesco"))
        line = places.line(51.5, 0.02)
        self.assertTrue(line.startswith("🚶 "))
        self.assertIn("🛒 Tesco ~1 min", line)
        self.assertIn("🏪 Co-op b ~2 min", line)       # 100 m * 1.3 / 80 = 1.6 -> 2; <, > and & are stripped from names
        self.assertIn("🎬 Odeon ~", line)
        self.assertNotIn("Far Away", line)
        self.assertNotIn("Too Far", line)

    def test_minutes_formula(self):
        # gym ~334 m north: 334 * 1.3 / 80 = 5.4 -> 5
        self.assertIn("🏋 PureGym With… ~5 min", places.line(51.5, 0.02))

    def test_names_are_escaped_and_trimmed(self):
        line = places.line(51.5, 0.02)
        self.assertNotIn("<b>", line)
        self.assertNotIn("&amp;&", line)
        self.assertLessEqual(max(len(part) for part in line.split(" · ")), 40)

    def test_nothing_within_range_gives_empty_line(self):
        self.assertEqual(places.line(51.9, -0.5), "")

    def test_bad_input_and_bad_file_fail_open(self):
        for lat, lng in ((None, 0.0), (51.5, None), (float("nan"), 0.0), ("x", "y")):
            self.assertEqual(places.line(lat, lng), "")
        self.path.write_text("{not json", encoding="utf-8")
        places._state.update(loaded=False, grid=None)
        self.assertEqual(places.line(51.5, 0.02), "")
        self.path.unlink()
        places._state.update(loaded=False, grid=None)
        self.assertEqual(places.line(51.5, 0.02), "")

    def test_malformed_rows_are_skipped_not_fatal(self):
        fixture(self.path, {"supermarket": [["a", "b", "x"], [51.5, 0.02, None], [51.5, 0.02]]})
        places._state.update(loaded=False, grid=None)
        self.assertEqual(places.line(51.5, 0.02), "")      # whole file rejected, still "" not an error

    def test_flag_default_off(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PLACE_TILES", None)
            self.assertFalse(places.enabled())
        with patch.dict(os.environ, {"PLACE_TILES": "1"}):
            self.assertTrue(places.enabled())


class CardTests(PlaceCase):
    def test_off_card_is_identical(self):
        base = telegram.format_listing(home(), rank=1)
        l = home()
        self.assertEqual(telegram.format_listing(l, rank=1), base)
        l.place_line = ""
        self.assertEqual(telegram.format_listing(l, rank=1), base)

    def test_line_added_and_card_stays_one_message(self):
        l = home()
        l.place_line = places.line(51.5, 0.02)
        text = telegram.format_listing(l, rank=1)
        self.assertTrue(text.endswith(l.place_line))
        self.assertLessEqual(tgtext.units(text) + 70, tgtext.CAPTION)   # still fits with the header-unavailable note

    def test_long_card_skips_the_line_rather_than_splitting(self):
        l = home()
        l.ai_summary = "x" * 800
        base = telegram.format_listing(l, rank=1)
        l.place_line = "🚶 " + "y" * 60
        self.assertEqual(telegram.format_listing(l, rank=1), base)

    def test_boundary_never_pushes_a_one_message_card_over_the_limit(self):
        for pad in range(0, 900, 7):
            l = home()
            l.ai_summary = "x" * pad
            base = telegram.format_listing(l, rank=1)
            l.place_line = places.line(51.5, 0.02)
            text = telegram.format_listing(l, rank=1)
            if tgtext.units(base) <= tgtext.CAPTION - 70:
                self.assertLessEqual(tgtext.units(text), tgtext.CAPTION - 70, pad)
            else:
                self.assertEqual(text, base, pad)


class PerformanceAndPathTests(PlaceCase):
    def test_lookups_are_fast_after_first_load(self):
        places.line(51.5, 0.02)
        start = time.perf_counter()
        for _ in range(200):
            places.line(51.5, 0.02)
        self.assertLess((time.perf_counter() - start) / 200, 0.005)

    def test_shipped_data_file_is_valid_and_small(self):
        shipped = Path(places.__file__).resolve().parent / "data" / "place_tiles.json"
        if not shipped.exists():
            self.skipTest("place tiles not built")
        raw = json.loads(shipped.read_text(encoding="utf-8"))
        self.assertEqual(set(raw["points"]), {"supermarket", "convenience", "gym", "cinema"})
        self.assertLess(shipped.stat().st_size, 2_000_000)
        self.assertIn("ODbL", raw["meta"]["license"])

    def test_main_does_not_touch_cards_when_flag_off(self):
        self.assertTrue(hasattr(main, "places"))
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PLACE_TILES", None)
            self.assertFalse(main.places.enabled())


if __name__ == "__main__":
    unittest.main()
