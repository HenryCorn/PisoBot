import json
import math
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from pisobot import cache, place
from pisobot.models import Listing
from pisobot.telegram import format_listing
from scripts import eval_place


class PlaceFactsTests(unittest.TestCase):
    def test_local_index_reader_uses_fixture_sqlite_grid(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "fixture.sqlite3")
            db = sqlite3.connect(path)
            db.executescript("""
                CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE features (
                    id INTEGER PRIMARY KEY, kind TEXT NOT NULL, class TEXT, name TEXT,
                    osm_id TEXT NOT NULL, lat1 REAL, lon1 REAL, lat2 REAL, lon2 REAL,
                    lat REAL, lon REAL
                );
                CREATE TABLE grid_cells (
                    cell_x INTEGER NOT NULL, cell_y INTEGER NOT NULL, feature_id INTEGER NOT NULL,
                    PRIMARY KEY (cell_x, cell_y, feature_id)
                ) WITHOUT ROWID;
            """)
            db.executemany("INSERT INTO meta VALUES (?, ?)", [
                ("grid_cell_size_m", "250"),
                ("grid_lat_origin", "51.5"),
                ("grid_lon_scale", str(111320 * math.cos(math.radians(51.5)))),
            ])
            features = [
                (1, "road", "residential", None, "way/1", 51.5, -0.1005, 51.5, -0.0995, None, None),
                (2, "road", "primary", None, "way/2", 51.5002, -0.1005, 51.5002, -0.0995, None, None),
                (3, "railway", "rail", None, "way/3", 51.501, -0.1005, 51.501, -0.0995, None, None),
                (4, "amenity", "supermarket", "Lidl", "node/4", None, None, None, None, 51.5005, -0.1),
                (5, "amenity", "park", "Community Park", "way/5", None, None, None, None, 51.501, -0.1),
                (6, "amenity", "cafe", "Cafe", "node/6", None, None, None, None, 51.502, -0.1),
            ]
            db.executemany("INSERT INTO features VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", features)
            cell_size = 250
            lon_scale = 111320 * math.cos(math.radians(51.5))
            cell_x = math.floor(-0.1 * lon_scale / cell_size)
            cell_y = 0
            db.executemany("INSERT INTO grid_cells VALUES (?, ?, ?)",
                           [(cell_x, cell_y, row[0]) for row in features])
            db.commit()
            db.close()

            facts = place.local_osm_facts(51.5, -0.1, path)

        self.assertEqual(facts["street"]["class"], "residential")
        self.assertEqual(facts["main_road"]["class"], "primary")
        self.assertGreater(facts["main_road"]["distance_m"], 0)
        self.assertLess(facts["main_road"]["distance_m"], 30)
        self.assertGreater(facts["railway"]["distance_m"], 100)
        self.assertEqual(facts["amenities"]["supermarket"]["nearest"]["name"], "Lidl")
        self.assertEqual(facts["amenities"]["park"]["count"], 1)
        self.assertEqual(place.street_label(facts), "Busy road nearby")

    def test_place_facts_are_unavailable_without_index(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = os.path.join(directory, "missing.sqlite3")
            with patch.object(place, "LOCAL_INDEX_PATH", missing), \
                    patch.object(place, "_tfl") as tfl, patch.object(place, "_noise") as noise:
                self.assertIsNone(place.facts(51.5, -0.1))
                tfl.assert_not_called()
                noise.assert_not_called()

    def test_tfl_counts_distinct_lines_and_bus_routes_with_distance_limits(self):
        data = {"stopPoints": [
            {"distance": 200, "modes": ["tube", "bus"],
             "lines": [{"id": "Central"}, {"id": "8"}]},
            {"distance": 250, "modes": ["tube", "bus"],
             "lines": [{"id": "Central"}, {"id": "25"}]},
            {"distance": 450, "modes": ["bus"], "lines": [{"id": "N8"}]},
            {"distance": 850, "modes": ["tube"], "lines": [{"id": "Victoria"}]},
        ]}
        self.assertEqual(place.parse_tfl(data), {"rail_lines": 1, "bus_routes": 2})
        self.assertIn("stopTypes=NaptanPublicBusCoachTram,NaptanMetroStation,NaptanRailStation",
                      place._tfl_url(51.5, -0.1))
    def test_facts_use_cache_and_refresh_after_ttl(self):
        cache._data = {}
        self.addCleanup(setattr, cache, "_data", {})
        osm = {
            "street": {"class": "residential", "distance_m": 10},
            "main_road": None, "railway": None, "amenities": {},
        }
        with tempfile.NamedTemporaryFile() as index, \
                patch.object(place, "LOCAL_INDEX_PATH", index.name), \
                patch.object(place, "local_osm_facts", return_value=osm) as local, \
                patch.object(place, "_tfl", return_value={"stopPoints": []}) as tfl, \
                patch.object(place, "_noise", return_value={"road_lnight_db": 42.0,
                                                            "rail_lnight_db": None}) as noise:
            first = place.facts(51.5, -0.1, now=1000)
            second = place.facts(51.5001, -0.1001, now=1001)
            self.assertEqual(first, second)
            local.assert_called_once()
            tfl.assert_called_once()
            self.assertEqual(noise.call_count, 2)
            place.facts(51.5, -0.1, now=1000 + place.CACHE_TTL_SECONDS + 1)
            self.assertEqual(local.call_count, 2)

    def test_tfl_and_noise_failures_preserve_local_osm_facts(self):
        cache._data = {}
        self.addCleanup(setattr, cache, "_data", {})
        osm = {
            "street": {"class": "residential", "distance_m": 10},
            "main_road": None, "railway": None, "amenities": {},
        }
        with tempfile.NamedTemporaryFile() as index, \
                patch.object(place, "LOCAL_INDEX_PATH", index.name), \
                patch.object(place, "local_osm_facts", return_value=osm), \
                patch.object(place, "_tfl", side_effect=place.requests.Timeout("TfL timeout")), \
                patch.object(place, "_noise", side_effect=place.requests.Timeout("Defra timeout")):
            facts = place.facts(51.5, -0.1, now=2000)

        self.assertEqual(facts["street"], osm["street"])
        self.assertNotIn("transit", facts)
        self.assertNotIn("noise", facts)
        self.assertEqual(place.card_line(facts), "🏘 Residential street")
        self.assertFalse(any("Defra" in line for line in place.detail_lines(facts)))

    def test_noise_answers_are_cached_per_grid_cell_indefinitely(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "noise.sqlite3")

            class Response:
                status_code = 200
                headers = {}
                text = "GRAY_INDEX = 42.5"

                @staticmethod
                def raise_for_status():
                    return None

            with patch.object(place, "NOISE_CACHE_PATH", path), \
                    patch.object(place.requests, "get", return_value=Response()) as get:
                first = place._noise_point("https://example.test/wms", "road", 51.5, -0.1)
                second = place._noise_point("https://example.test/wms", "road",
                                            51.5001, -0.1001)

        self.assertEqual(first, 42.5)
        self.assertEqual(second, 42.5)
        get.assert_called_once()

    def test_noise_failure_is_not_retried_for_same_cell_during_backoff(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "noise.sqlite3")
            with patch.object(place, "NOISE_CACHE_PATH", path), \
                    patch.object(place.time, "time", return_value=1000), \
                    patch.object(place.requests, "get",
                                 side_effect=place.requests.Timeout("Defra timeout")) as get:
                with self.assertRaises(place.requests.Timeout):
                    place._noise_point("https://example.test/wms", "road", 51.5, -0.1)
                skipped = place._noise_point("https://example.test/wms", "road",
                                             51.5001, -0.1001)

        self.assertIs(skipped, place._NOISE_UNAVAILABLE)
        get.assert_called_once()

    def test_noise_429_honors_retry_after_and_sets_long_persistent_backoff(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "noise.sqlite3")

            class Response:
                status_code = 429
                headers = {"Retry-After": "86400"}
                text = ""

                @staticmethod
                def raise_for_status():
                    raise AssertionError("429 is handled before raise_for_status")

            with patch.object(place, "NOISE_CACHE_PATH", path), \
                    patch.object(place, "NOISE_RATE_LIMIT_BACKOFF_SECONDS", 60), \
                    patch.object(place.time, "time", return_value=1000), \
                    patch.object(place.requests, "get", return_value=Response()):
                with self.assertRaises(place.NoiseRateLimited):
                    place._noise_point("https://example.test/wms", "road", 51.5, -0.1)
                self.assertTrue(place.noise_backoff_active())

            db = sqlite3.connect(path)
            blocked_until = db.execute(
                "SELECT value FROM noise_state WHERE key = 'blocked_until'"
            ).fetchone()[0]
            db.close()
        self.assertGreaterEqual(blocked_until, 1000 + 86400)

    def test_noise_timeout_does_not_prevent_other_layer_lookup(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(place, "NOISE_CACHE_PATH",
                              os.path.join(directory, "noise.sqlite3")), \
                    patch.object(place, "_noise_point",
                                 side_effect=[place.requests.Timeout("road timeout"), 47.1]) as point:
                noise = place._noise(51.5, -0.1)
        self.assertEqual(noise, {"rail_lnight_db": 47.1})
        self.assertEqual(point.call_count, 2)

    def test_defra_point_value_parser_ignores_zero_and_errors(self):
        self.assertEqual(place._noise_value("GRAY_INDEX = 42.5"), 42.5)
        self.assertIsNone(place._noise_value("GRAY_INDEX = 0.0"))
        self.assertIsNone(place._noise_value("No features found"))

    def test_street_labels_respect_distance_and_noise_bands(self):
        self.assertEqual(place.noise_band(44.9), "low")
        self.assertEqual(place.noise_band(45), "moderate")
        self.assertEqual(place.noise_band(55), "moderate")
        self.assertEqual(place.noise_band(55.1), "high")
        facts = {
            "street": {"class": "residential", "distance_m": 20},
            "main_road": {"class": "primary", "distance_m": 180},
            "railway": None,
            "noise": {"road_lnight_db": 46, "rail_lnight_db": None},
        }
        self.assertEqual(place.street_label(facts), "Busy road nearby")
        self.assertEqual(place.card_line(facts), "🏘 Busy road nearby · night noise moderate")
        facts["main_road"]["distance_m"] = 301
        self.assertEqual(place.street_label(facts), "Residential street")
        facts["noise"]["road_lnight_db"] = 44
        self.assertEqual(place.street_label(facts), "Quiet residential street")

    def test_street_label_mentions_road_and_rail_together_without_unknown_noise(self):
        facts = {
            "street": {"class": "residential", "distance_m": 20},
            "main_road": {"class": "primary", "distance_m": 70},
            "railway": {"distance_m": 86},
        }
        self.assertEqual(place.street_label(facts), "Busy road and railway nearby")
        self.assertEqual(place.card_line(facts), "🏘 Busy road and railway nearby")

    def test_partial_noise_data_does_not_claim_a_noise_band(self):
        facts = {
            "street": {"class": "residential", "distance_m": 20},
            "main_road": {"class": "trunk", "distance_m": 78},
            "railway": {"distance_m": 221},
            "noise": {"rail_lnight_db": 40.7},
        }
        self.assertIsNone(place._night_noise(facts))
        self.assertEqual(place.card_line(facts), "🏘 Busy road nearby")

    def test_card_adds_place_line_without_changing_cards_without_facts(self):
        listing = Listing(source="rm", id="1", url="https://example.test", address="London N7",
                          bedrooms=1, scheme="Private rent", tenure="rent", rent=1300)
        listing.travel_minutes = 20
        listing.place_facts = {
            "street": {"class": "residential", "distance_m": 30},
            "main_road": {"class": "primary", "distance_m": 550},
            "railway": None,
            "amenities": {"supermarket": {"count": 1, "nearest": {"name": "Lidl", "distance_m": 250}}},
            "transit": {"rail_lines": 2, "bus_routes": 9},
            "noise": {"road_lnight_db": 42.0, "rail_lnight_db": None},
        }
        rendered = format_listing(listing)
        line = next(row for row in rendered.splitlines() if row.startswith("🏘"))
        self.assertEqual(line, "🏘 Quiet residential street · night noise low · 2 rail/tube lines · Lidl ~250m")
        self.assertLessEqual(len(line), 90)
        details = place.detail_lines(listing.place_facts)
        self.assertTrue(any("primary (~550 m)" in row for row in details))
        self.assertTrue(any("Lnight (2021 model year, 4 m receptor" in row for row in details))
        listing.place_facts = {}
        self.assertNotIn("🏘", format_listing(listing))

    def test_evaluation_reports_provider_failures_and_returns_nonzero(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "cache.json")
            with open(path, "w", encoding="utf-8") as file:
                json.dump({"rent_search": {"x": {"listings": [
                    {"lat": 51.5, "lng": -0.1}, {"lat": 51.6, "lng": -0.2},
                ]}}}, file)
            output, delays = [], []

            def unavailable(_lat, _lng):
                raise TimeoutError("fixture timeout")

            with patch.object(place, "noise_backoff_active", return_value=False):
                result = eval_place.main(
                    ["--cache", path, "--sample", "2"], out=output.append,
                    facts_for=unavailable, sleep=delays.append,
                )
        self.assertEqual(result, 1)
        self.assertEqual(delays, [10])
        self.assertEqual(sum("Place facts unavailable" in row for row in output), 2)
        self.assertTrue(any("TimeoutError: fixture timeout" in row for row in output))

    def test_evaluation_can_select_original_coordinate_indices_and_stops_on_429(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "cache.json")
            with open(path, "w", encoding="utf-8") as file:
                json.dump({"rent_search": {"x": {"listings": [
                    {"lat": 51.5 + n / 100, "lng": -0.1} for n in range(4)
                ]}}}, file)
            output, queried = [], []

            def partial_facts(lat, _lng):
                queried.append(lat)
                return {"street": {"class": "residential", "distance_m": 10}}

            with patch.object(place, "noise_backoff_active", return_value=True):
                result = eval_place.main(
                    ["--cache", path, "--indices", "2,4"], out=output.append,
                    facts_for=partial_facts, sleep=lambda _delay: self.fail("must stop after 429"),
                )
        self.assertEqual(result, 0)
        self.assertEqual(queried, [51.51])
        self.assertTrue(any(row.startswith("02.") for row in output))
        self.assertTrue(any("stopping the remaining" in row for row in output))


if __name__ == "__main__":
    unittest.main()
