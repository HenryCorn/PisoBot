import json
import math
import os
import sqlite3
import stat
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from pisobot import optional, place
from pisobot.optional import store
from pisobot.place_feature import PlaceFeature, _overview_text, area_reply
from scripts import eval_place


class PlaceFactsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        self.store_path = patch.object(store, "_state", return_value=self.state)
        self.store_path.start()
        self.addCleanup(self.store_path.stop)

    @staticmethod
    def make_index(path):
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
            (4, "amenity", "supermarket", "Tesco", "node/4", None, None, None, None, 51.5, -0.0995),
            (5, "amenity", "convenience", "Co-op", "node/5", None, None, None, None, 51.5, -0.099),
            (6, "amenity", "gym", "PureGym", "node/6", None, None, None, None, 51.5, -0.098),
            (7, "amenity", "cinema", "Vue", "node/7", None, None, None, None, 51.5, -0.096),
            (8, "amenity", "park", "Community Park", "way/8", None, None, None, None, 51.501, -0.1),
            (9, "amenity", "cafe", "Cafe", "node/9", None, None, None, None, 51.502, -0.1),
            (10, "amenity", "pub", "The Pub", "node/10", None, None, None, None, 51.499, -0.1),
            (11, "amenity", "restaurant", "Restaurant", "node/11", None, None, None, None, 51.5, -0.101),
            (12, "amenity", "shop", "Market", "node/12", None, None, None, None, 51.5, -0.102),
            (13, "amenity", "supermarket", "Far Away", "node/13", None, None, None, None, 51.5, -0.13),
        ]
        db.executemany("INSERT INTO features VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", features)
        cell_x = math.floor(-0.1 * 111320 * math.cos(math.radians(51.5)) / 250)
        db.executemany("INSERT INTO grid_cells VALUES (?, ?, ?)",
                       [(cell_x, 0, row[0]) for row in features])
        db.commit()
        db.close()

    def test_local_index_reads_four_nearest_categories_and_area_counts(self):
        path = os.path.join(self.temp.name, "place-index.sqlite3")
        self.make_index(path)

        facts = place.local_osm_facts(51.5, -0.1, path)

        self.assertEqual(facts["amenities"]["supermarket"]["nearest"]["name"], "Tesco")
        self.assertEqual(facts["amenities"]["convenience"]["nearest"]["name"], "Co-op")
        self.assertEqual(facts["amenities"]["gym"]["nearest"]["name"], "PureGym")
        self.assertEqual(facts["amenities"]["cinema"]["nearest"]["name"], "Vue")
        self.assertEqual(facts["amenities"]["supermarket"]["count"], 1)
        self.assertEqual(facts["amenities"]["cafe"]["count"], 1)
        self.assertIsNone(place.local_osm_facts(51.5, -0.1, path)["amenities"]["cinema"].get("far"))
        self.assertEqual(facts["main_road"]["class"], "primary")

    def test_amenities_tile_is_one_line_bounded_and_omits_missing_categories(self):
        facts = {
            "amenities": {
                "supermarket": {"nearest": {"name": "Tesco Extra with a very long branch name", "distance_m": 240}},
                "convenience": {"nearest": {"name": "Co-op", "distance_m": 120}},
                "gym": {"nearest": {"name": "PureGym", "distance_m": 400}},
                "cinema": {"nearest": {"name": "Vue", "distance_m": 800}},
            },
        }

        line = place.amenities_line(facts)

        self.assertIn("🛒 Tesco Extra with… ~4 min", line)
        self.assertIn("🏪 Co-op ~2 min", line)
        self.assertIn("🏋 PureGym ~7 min", line)
        self.assertIn("🎬 Vue ~13 min", line)
        self.assertNotIn("\n", line)
        self.assertLessEqual(len(line), 160)
        self.assertEqual(place.amenities_line({"amenities": {"supermarket": {"nearest": None}}}), "")

    def test_name_truncation_preserves_word_boundary(self):
        self.assertEqual(place._short_name("McKenna Green Estate"), "McKenna Green…")
        self.assertEqual(place._short_name("LongUnbrokenShopName"), "LongUnbrokenShopN…")

    def test_index_builder_excludes_explicitly_underground_rail_and_indexes_requested_categories(self):
        stub_osmium = types.SimpleNamespace(SimpleHandler=type("SimpleHandler", (), {}))
        with patch.dict("sys.modules", {"osmium": stub_osmium}):
            from scripts import build_place_index

        self.assertTrue(build_place_index._surface_railway({"railway": "rail"}))
        for tags in (
                {"railway": "rail", "location": "underground"},
                {"railway": "rail", "layer": "-1"},
                {"railway": "rail", "tunnel": "yes"},
                {"railway": "rail", "disused": "yes"},
                {"railway": "subway"}):
            self.assertFalse(build_place_index._surface_railway(tags))
        self.assertEqual(build_place_index._amenity_category({"shop": "convenience"}), "convenience")
        self.assertEqual(build_place_index._amenity_category({"amenity": "cinema"}), "cinema")
        self.assertEqual(build_place_index._amenity_category({"amenity": "pub"}), "pub")
        self.assertEqual(build_place_index._amenity_category({"shop": "books"}), "shop")

    def test_index_builder_keeps_rebuilt_index_readable_to_service_group(self):
        from scripts import build_place_index

        extract = Path(self.temp.name) / "extract.pbf"
        extract.touch()
        output = Path(self.temp.name) / "place-index.sqlite3"
        output.write_text("old index")
        output.chmod(0o600)

        def add_fixture_feature(handler, *_args, **_kwargs):
            handler.writer.add("amenity", "cinema", "Vue", "node/1", lat=51.5, lon=-0.1)

        with patch.object(build_place_index._PlaceHandler, "apply_file", add_fixture_feature, create=True), \
                patch.object(build_place_index, "_header_timestamp", return_value=None):
            build_place_index.build(extract, output)

        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o640)
        db = sqlite3.connect(output)
        try:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM features").fetchone()[0], 1)
        finally:
            db.close()

    def test_defra_parser_distinguishes_valid_no_data_from_http_200_errors(self):
        self.assertIsNone(place._noise_value("Results for FeatureType 'road':\n---"))
        self.assertEqual(place._noise_value("Results for FeatureType 'road':\nGRAY_INDEX = 42.5"), 42.5)
        for body in ("<ServiceExceptionReport><ServiceException>bad</ServiceException></ServiceExceptionReport>",
                     "upstream error", ""):
            with self.assertRaises(place.requests.RequestException):
                place._noise_value(body)

    def test_defra_http_200_error_is_not_cached_and_uses_failure_backoff(self):
        class Response:
            status_code = 200
            text = "<ServiceExceptionReport><ServiceException>upstream failed</ServiceException></ServiceExceptionReport>"

            @staticmethod
            def raise_for_status():
                return None

        with patch.object(place.time, "time", return_value=1000), \
                patch.object(place.requests, "get", return_value=Response()) as get:
            with self.assertRaises(place.requests.RequestException):
                place._noise_point("https://example.test/wms", "road", 51.5, -0.1)
            skipped = place._noise_point("https://example.test/wms", "road", 51.5, -0.1)

        self.assertIs(skipped, place._NOISE_UNAVAILABLE)
        self.assertEqual(store.read("SELECT * FROM place_noise_answers"), [])
        self.assertEqual(get.call_count, 1)
        self.assertTrue(store.read("SELECT * FROM place_noise_failures"))

    def test_optional_db_write_lock_is_not_held_during_defra_http(self):
        class Response:
            status_code = 200
            text = "Results for FeatureType 'road':\nGRAY_INDEX = 41.0"

            @staticmethod
            def raise_for_status():
                return None

        def during_request(*_args, **_kwargs):
            self.assertTrue(store.write(place.PLACE_SCHEMA, [
                ("INSERT INTO place_noise_state (key, value) VALUES ('probe', 1) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", ())
            ]))
            return Response()

        with patch.object(place.requests, "get", side_effect=during_request):
            self.assertEqual(place._noise_point("https://example.test/wms", "road", 51.5, -0.1), 41.0)

    def test_tfl_failure_is_backed_off_in_the_optional_database(self):
        with patch.object(place.time, "time", return_value=1000), \
                patch.object(place, "_tfl", side_effect=place.requests.Timeout("fixture")) as get:
            place._refresh_transit(51.5, -0.1, now=1000)
            place._refresh_transit(51.5, -0.1, now=1001)

        self.assertEqual(get.call_count, 1)
        self.assertTrue(store.read("SELECT * FROM place_transit_failures"))
        self.assertTrue(all(path.name.startswith("optional.db") for path in self.state.iterdir()))
        self.assertFalse(any("place-noise" in path.name or "pisobot.db" in path.name
                             for path in self.state.iterdir()))

    def test_card_facts_fail_open_for_malformed_optional_cache_shapes(self):
        malformed = {"amenities": {"supermarket": {"nearest": [1]}}}
        self.assertFalse(place._valid_facts(malformed))
        self.assertEqual(place.card_line(malformed), "")
        self.assertEqual(place.amenities_line(malformed), "")

    def test_overview_template_has_three_sentences_and_explicit_sample_bases(self):
        record = {
            "sample_size": 5, "green_space_share_pct": 60,
            "poi_density_per_location": {"cafes": 2.0, "pubs": 1.0, "restaurants": 0.5, "shops": 4.0},
            "main_road_within_200m_pct": 20, "surface_rail_within_100m_pct": 40,
            "tfl_rail_lines_mean": 2.0, "median_lnight_db": 45.0, "noise_sample_size": 3,
            "one_bed_median_rent": 1500, "one_bed_advert_count": 8,
            "crime_median": 100, "crime_comparison_median": 60, "crime_comparison_area_count": 4,
        }
        text = _overview_text(record)
        self.assertEqual(text.count(". "), 2)
        self.assertIn("60% of 5 sampled advert locations", text)
        self.assertIn("sampled-area median", text)
        self.assertIn("not a London-wide or population-adjusted risk measure", text)

    def test_area_overview_is_cached_and_read_only_command_accepts_a_full_postcode(self):
        from pisobot import place_feature

        facts = {
            "street": None, "main_road": {"class": "primary", "distance_m": 40},
            "railway": None,
            "amenities": {"park": {"count": 1, "nearest": {"name": "Park", "distance_m": 100}}},
        }
        rows = [
            {"key": "rm:1", "address": "London N7 1AB", "lat": 51.56, "lng": -0.12,
             "rent": 1500, "bedrooms": 1, "crime_score": 40},
            {"key": "rm:2", "address": "London N7 2AB", "lat": 51.561, "lng": -0.121,
             "rent": 1600, "bedrooms": 1, "crime_score": 60},
            {"key": "rm:3", "address": "London E1 1AA", "lat": 51.52, "lng": -0.06,
             "rent": 1700, "bedrooms": 1, "crime_score": 80},
        ]
        index = Path(self.temp.name) / "index.sqlite3"
        index.touch()
        with patch.object(place, "LOCAL_INDEX_PATH", str(index)), \
                patch.object(place_feature, "_load_rows", return_value=rows), \
                patch.object(place, "local_osm_facts", return_value=facts):
            feature = PlaceFeature()
            feature._rebuild()
            output = area_reply("N7 1AB")
            feature._rebuild()

        self.assertIn("<b>Area: N7 Holloway</b>", output)
        self.assertIn("1-bed advertised median", output)
        self.assertEqual(len(store.read("SELECT area FROM place_areas WHERE area = 'N7'")), 1)

    def test_area_command_handles_missing_and_invalid_cache_without_network(self):
        self.assertIn("Use /area", area_reply("not a postcode"))
        self.assertIn("No cached overview", area_reply("N7"))
        self.assertFalse(store.exists())

    def test_feature_flag_off_registers_no_worker_or_database(self):
        with patch.dict(os.environ, {"PLACE_FACTS": "0"}):
            optional.start()
            optional.after_ack(
                type("Listing", (), {"key": "rm:1", "lat": 51.5, "lng": -0.1})(),
                "owner", None, {})
        self.assertEqual(optional.workers(), [])
        self.assertFalse(store.exists())

    def test_evaluation_prints_tiles_and_overviews_without_network_or_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            cache_path = os.path.join(directory, "cache.json")
            index_path = os.path.join(directory, "index.sqlite3")
            with open(cache_path, "w", encoding="utf-8") as target:
                json.dump({"rent_search": {"x": {"listings": [
                    {"lat": 51.5, "lng": -0.1, "address": "London N7 1AB", "rent": 1500,
                     "bedrooms": 1},
                    {"lat": 51.51, "lng": -0.11, "address": "London N7 2AB", "rent": 1600,
                     "bedrooms": 1},
                ]}}}, target)
            output = []
            facts = {
                "main_road": None, "railway": None,
                "amenities": {
                    "supermarket": {"count": 1, "nearest": {"name": "Lidl", "distance_m": 200}},
                    "park": {"count": 1, "nearest": {"name": "Park", "distance_m": 100}},
                },
            }
            with patch.object(place, "local_osm_facts", return_value=facts) as lookup:
                result = eval_place.main(
                    ["--cache", cache_path, "--index", index_path, "--sample", "2"],
                    out=output.append)
        self.assertEqual(result, 0)
        self.assertEqual(lookup.call_count, 2)
        self.assertTrue(any("🛒 Lidl ~4 min" in line for line in output))
        self.assertTrue(any(line.startswith("    Area: N7") for line in output))


if __name__ == "__main__":
    unittest.main()
