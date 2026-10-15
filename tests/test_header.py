import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image, ImageChops

from pisobot import collage
from pisobot.models import Listing


def listing(**fields):
    return Listing("stb", "1", "https://example.com", "Home E16", 1, "SO",
                   **{"lat": 51.501, "lng": 0.028, **fields})


class MapTests(unittest.TestCase):
    def test_floorplan_is_contained_without_cropping_and_transparency_is_white(self):
        raw = io.BytesIO()
        original = Image.new("RGBA", (120, 400), (0, 0, 0, 0))
        original.putpixel((0, 0), (255, 0, 0, 255))
        original.putpixel((119, 399), (0, 0, 255, 255))
        original.save(raw, "PNG")
        with patch.object(collage.net, "get", return_value=Mock(content=raw.getvalue())):
            image = collage._thumb("https://example.com/plan.png", floorplan=True)
        self.assertEqual(image.size, (720, 500))
        self.assertEqual(image.getpixel((360, 250)), (255, 255, 255))
        self.assertIsNotNone(ImageChops.subtract(image.getchannel("R"), image.getchannel("G")).getbbox())
        self.assertIsNotNone(ImageChops.subtract(image.getchannel("B"), image.getchannel("G")).getbbox())

    def test_failed_plan_falls_back_to_hero_and_filename_detection_avoids_open_plan(self):
        self.assertTrue(collage._is_floorplan("https://example.com/floor-plan-0.png"))
        self.assertTrue(collage._is_floorplan("https://example.com/plan_1.jpg"))
        self.assertFalse(collage._is_floorplan("https://example.com/open-plan-living.jpg"))
        l = listing(floorplan="https://example.com/plan.png", image="https://example.com/hero.jpg")
        with patch.object(collage, "_map", return_value=None), \
                patch.object(collage, "_thumb", side_effect=[None, Image.new("RGB", (720, 500))]) as thumb:
            self.assertIsNotNone(collage.build(l))
        self.assertEqual(thumb.call_args_list[1].args, (l.image,))

    def test_view_fits_both_pins_and_preserves_north_up_orientation(self):
        for lat, lng in ((51.501, 0.028), (51.60, -0.25), (51.4, -0.20), (51.5152, -0.1419)):
            _, _, points = collage._map_view(listing(lat=lat, lng=lng))
            for x, y in points:
                self.assertGreaterEqual(x, 63)
                self.assertLessEqual(x, 417)
                self.assertGreaterEqual(y, 63)
                self.assertLessEqual(y, 365)
            a, b = points
            self.assertEqual(b[0] >= a[0], lng >= -0.1419)
            self.assertEqual(b[1] >= a[1], lat <= 51.5152)

    def test_map_is_attributed_and_network_failure_is_explicit(self):
        with patch.object(collage, "_tile", return_value=Image.new("RGB", (256, 256), "lightgrey")):
            result = collage._map(listing())
        self.assertEqual(result.size, (480, 500))
        self.assertNotEqual(result.getpixel((20, 480)), result.getpixel((20, 400)))
        with patch.object(collage, "_tile", side_effect=OSError("offline")), \
                patch("sys.stderr", new=io.StringIO()) as errors:
            self.assertIsNone(collage._map(listing()))
        self.assertIn("Map unavailable", errors.getvalue())

    def test_osm_tiles_are_cached_for_seven_days_with_identifiable_user_agent(self):
        raw = io.BytesIO()
        Image.new("RGB", (256, 256), "grey").save(raw, "PNG")
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(collage, "TILE_CACHE", Path(directory) / "tiles"), \
                patch.object(collage.net, "get", return_value=Mock(content=raw.getvalue())) as get:
            collage._tile(12, 2046, 1362)
            collage._tile(12, 2046, 1362)
            self.assertEqual(get.call_count, 1)
            self.assertIn("PisoBot", get.call_args.kwargs["headers"]["User-Agent"])
            with patch.object(collage.time, "time", return_value=10 ** 12):
                collage._tile(12, 2046, 1362)
            self.assertEqual(get.call_count, 2)
