import fcntl
import hashlib
import json
import math
import os
import sqlite3
import stat
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from pisobot import archive, config, main, optional, place, place_feature, sweep, telegram, users, watch
from pisobot.optional import store
from pisobot.place_feature import PlaceFeature, _area_record, _overview_text, area_reply, district_for_address
from pisobot.sources import rightmove
from scripts import eval_place

try:
    from tests.test_sweep import JudgingBase
    from tests.test_archive import advert
except ImportError:
    from test_sweep import JudgingBase
    from test_archive import advert


class PlaceFactsTests(unittest.TestCase):
    FACTS = {"street": None, "main_road": None, "railway": None, "amenities": {
        "supermarket": {"count": 1, "nearest": {"name": "Lidl", "distance_m": 200}},
        "park": {"count": 1, "nearest": {"name": "Garden", "distance_m": 100}}}}

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        self.store_path = patch.object(store, "_state", return_value=self.state)
        self.store_path.start()
        self.addCleanup(self.store_path.stop)
        self.enterContext(patch.dict(os.environ, {"PISOBOT_STATE_DIR": self.temp.name, "STORAGE": "local"}))
        self.enterContext(patch.object(place.requests, "get", side_effect=AssertionError("live provider request")))
        self.enterContext(patch.object(place, "_tfl_next", 0))
        optional.reset()
        self.addCleanup(optional.reset)

    @staticmethod
    def rows(n, area="N7", prefix="rm", crime=None):
        return [{"key": f"{prefix}:{i}", "address": f"Holloway Road, London {area}",
                 "lat": 51.52 + i * 0.001, "lng": -0.12, "rent": 1400 + i,
                 "bedrooms": 1, "crime_score": crime} for i in range(n)]

    def rebuild(self, feature, rows, facts=None):
        index = self.state / "index.sqlite3"
        if not index.exists():
            self.make_index(index)
        with patch.object(place, "LOCAL_INDEX_PATH", str(index)), \
                patch.object(place_feature, "_load_rows", return_value=rows), \
                patch.object(place, "local_osm_facts", return_value=facts or self.FACTS):
            feature._rebuild()
            feature.has_work()
        return index

    def make_archive(self, n):
        path = self.state / "pisobot.db"
        db = sqlite3.connect(path)
        try:
            db.executescript(archive.DDL)
            db.executemany(
                "INSERT INTO adverts (key, source, address, lat, lng, rent, bedrooms, first_seen, last_seen, data) "
                "VALUES (?, 'rm', 'London N7', 51.52, -0.12, 1500, 1, '2026-11-11', '2026-11-11', ?)",
                [(f"rm:{i:05d}", json.dumps({"description": "x" * 3000})) for i in range(n)])
            db.executemany(
                "INSERT INTO verdicts (chat_id, key, outcome, crime_score, settings, updated_at) "
                "VALUES (?, ?, 'matched', 40, '{}', '2026-11-11')",
                [(str(person), f"rm:{i:05d}") for person in range(2) for i in range(n)])
            db.commit()
        finally:
            db.close()
        return path

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
        rows = self.rows(5, crime=100)
        record = _area_record("N7", rows, {row["key"]: self.FACTS for row in rows},
                              {}, [40, 60, 100], time.time())
        text = _overview_text(record)
        self.assertEqual(text.count(". "), 2)
        self.assertIn("sample of 5 of 5 advert locations", text)
        self.assertIn("not representative", text)
        self.assertIn("already pass users' crime caps", text)
        self.assertIn("not London-wide or population-adjusted risk", text)
        self.assertIn("gardens and recreation grounds", text)
        self.assertIn("not park access or land-area coverage", text)

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
        self.assertIn("only 2 one-bed", output)
        self.assertIn("no rent median", output)
        self.assertIn("Cached 20", output)
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

    def test_primary_read_scales_and_does_not_modify_the_archive(self):
        path = self.make_archive(8000)
        before = hashlib.sha256(path.read_bytes()).digest()
        rows = place_feature._load_rows()
        self.assertIsNotNone(rows)
        self.assertEqual(len(rows), place_feature.ARCHIVE_LIMIT)
        self.assertTrue(all(row["crime_score"] == 40 for row in rows))
        self.assertEqual(hashlib.sha256(path.read_bytes()).digest(), before)
        db = sqlite3.connect(path)
        try:
            plan = [row[3] for row in db.execute("EXPLAIN QUERY PLAN " + place_feature.ARCHIVE_SQL)]
        finally:
            db.close()
        self.assertFalse(any("CORRELATED" in operation for operation in plan))

    def test_primary_timeout_and_valid_empty_have_different_results(self):
        self.make_archive(0)
        self.assertEqual(place_feature._load_rows(), [])
        with patch.object(store, "read_primary", return_value=[]), self.assertLogs(place_feature.log, "WARNING"):
            self.assertIsNone(place_feature._load_rows())

    def test_real_primary_query_interrupt_is_not_a_successful_empty_read(self):
        self.make_archive(2000)
        with patch.object(store, "READ_SECONDS", 0), self.assertLogs(place_feature.log, "WARNING"):
            self.assertIsNone(place_feature._load_rows())

    def test_failed_archive_read_preserves_cache_but_valid_empty_prunes_it(self):
        feature = PlaceFeature()
        self.rebuild(feature, self.rows(5))
        self.rebuild(feature, None)
        self.assertEqual(len(store.read("SELECT key FROM place_advert_facts")), 5)
        self.rebuild(feature, [])
        self.assertEqual(store.read("SELECT key FROM place_advert_facts"), [])
        self.assertEqual(store.read("SELECT area FROM place_areas"), [])
        self.assertEqual(optional._snapshot, {})

    def test_districts_use_last_valid_candidate_and_normalise_central_outcodes(self):
        cases = {
            "Old Street, London, EC1V": "EC1", "Victoria Street, London SW1E 5JL": "SW1",
            "Marylebone High Street, London, W1U": "W1", "Wapping High Street, London, E1W": "E1",
            "Gray's Inn Road, WC1X": "WC1", "London N1C 4AA": "N1C",
            "Flat E2, Rye Lane, Peckham, London SE15": "SE15",
            "Unit N1, Coldharbour Lane, Brixton, SW9": "SW9",
            "Flat A1, Holloway Road, London N7": "N7",
            "Flat E2, London": None, "London N7Q": None, "London XX99": None,
            "No postcode": None,
        }
        for address, expected in cases.items():
            with self.subTest(address=address):
                self.assertEqual(district_for_address(address), expected)
        for query, district in (("ec1v9hx", "EC1"), ("SW1A 1AA", "SW1"), ("W1T 4TP", "W1")):
            self.assertIn(f"No cached overview for {district}", area_reply(query))
        self.assertIn("Use /area", area_reply("Flat E2"))
        self.assertIn("Use /area", area_reply("N7 extra"))

    def test_small_samples_are_not_area_or_crime_claims_and_growing_samples_refresh(self):
        feature = PlaceFeature()
        self.rebuild(feature, self.rows(1, crime=80))
        first = area_reply("N7")
        self.assertIn("too sparse", first)
        self.assertNotIn("100%", first)
        self.assertNotIn("above", first)
        self.assertIn("only 1 locations, too sparse", optional._snapshot["rm:0"][-1][1])
        self.rebuild(feature, self.rows(15, crime=80))
        later = area_reply("N7")
        self.assertNotEqual(first, later)
        self.assertIn("sample of 15 of 15", later)
        self.assertIn("15 advert locations, biased sample", optional._snapshot["rm:0"][-1][1])
        self.assertNotIn("parks within", later)

    def test_provider_arrival_updates_a_current_area_instead_of_waiting_30_days(self):
        feature, rows = PlaceFeature(), self.rows(5)
        self.rebuild(feature, rows)
        self.assertNotIn("cached TfL mean", area_reply("N7"))
        statements = []
        for row in rows:
            cell = place._cell(row["lat"], row["lng"])
            statements.append(("INSERT INTO place_transit_answers VALUES (?, ?, ?)",
                               (cell, time.time(), '{"rail_lines": 2, "bus_routes": 4}')))
            for _, layer, _ in place.NOISE_SERVICES:
                statements.append(("INSERT INTO place_noise_answers VALUES (?, ?, 42, ?)",
                                   (cell, layer, time.time())))
        self.assertTrue(store.write("", statements))
        self.rebuild(feature, rows)
        reply = area_reply("N7")
        self.assertIn("cached TfL mean 2 rail lines from 5 locations", reply)
        self.assertIn("cached Defra median Lnight 42 dB", reply)

    def test_crime_comparisons_require_distinct_locations_and_disclose_filter_bias(self):
        rows = self.rows(5, crime=80)
        duplicate_locations = [{**row, "lat": 51.52} for row in rows]
        record = _area_record("N7", duplicate_locations, {row["key"]: self.FACTS for row in rows},
                              {}, [20, 30, 40], time.time())
        self.assertEqual(record["crime_sample_size"], 1)
        self.assertIsNone(record["crime_median"])
        self.assertNotIn("are above", record["text"])
        record = _area_record("N7", rows, {row["key"]: self.FACTS for row in rows},
                              {}, [20, 30, 40], time.time())
        self.assertIn("are above the median", record["text"])
        self.assertIn("selection-biased", record["text"])
        self.assertNotIn("crime", place_feature._area_line(record))

    def test_expired_or_wrong_shaped_area_cache_is_not_served_and_is_repaired(self):
        feature, rows = PlaceFeature(), self.rows(5)
        self.rebuild(feature, rows)
        self.assertTrue(store.write("", [("UPDATE place_areas SET generated_at = ?", (time.time() - 400 * 86400,))]))
        self.assertIn("expired", area_reply("N7"))
        for value in ("[1]", '{"version":2,"area":[]}', "null"):
            self.assertTrue(store.write("", [
                ("UPDATE place_areas SET value = ?, generated_at = ?", (value, time.time()))]))
            with self.assertLogs(place_feature.log, "WARNING"):
                self.assertIn("No cached overview", area_reply("N7"))
                self.rebuild(feature, rows)
            self.assertEqual(len(optional._snapshot), 5)
            self.assertIn("Area: N7", area_reply("N7"))

    def test_removed_adverts_areas_and_provider_cells_are_pruned(self):
        feature = PlaceFeature()
        self.rebuild(feature, self.rows(50))
        cell = place._cell(51.52, -0.12)
        self.assertTrue(store.write("", [(
            "INSERT INTO place_noise_answers VALUES (?, ?, 42, ?)",
            (cell, place.NOISE_SERVICES[0][1], time.time() - place.CACHE_TTL_SECONDS - 1))]))
        replacement = [{**row, "lat": row["lat"] + 0.1} for row in self.rows(50, "SE15", "new")]
        self.rebuild(feature, replacement)
        self.assertEqual(len(store.read("SELECT key FROM place_advert_facts")), 50)
        self.assertEqual(store.read("SELECT area FROM place_areas"), [{"area": "SE15"}])
        self.assertEqual(store.read("SELECT cell FROM place_noise_answers"), [])
        self.assertNotIn("rm:0", optional._snapshot)

    def test_corrected_coordinates_invalidate_advert_facts_immediately(self):
        feature, rows = PlaceFeature(), self.rows(1)
        self.rebuild(feature, rows)
        updated = [{**rows[0], "lat": 51.60}]
        facts = {**self.FACTS, "amenities": {"supermarket": {
            "count": 1, "nearest": {"name": "New supermarket", "distance_m": 80}}}}
        self.rebuild(feature, updated, facts)
        self.assertIn("New supermarket", optional._snapshot["rm:0"][0][1])
        self.assertEqual(store.read("SELECT lat FROM place_advert_facts")[0]["lat"], 51.60)

    def test_old_noise_schema_is_invalidated_without_resetting_provider_backoff(self):
        db = sqlite3.connect(store.path())
        try:
            db.executescript("""
                CREATE TABLE place_noise_answers (cell TEXT, layer TEXT, value REAL);
                INSERT INTO place_noise_answers VALUES ('old', 'road', NULL);
                CREATE TABLE place_noise_state (key TEXT PRIMARY KEY, value REAL NOT NULL);
            """)
            db.execute("INSERT INTO place_noise_state VALUES ('blocked_until', ?)", (time.time() + 86400,))
            db.commit()
        finally:
            db.close()
        place._ensure_cache()
        self.assertEqual(store.read("SELECT cell, fetched_at FROM place_noise_answers"), [])
        self.assertTrue(place.noise_backoff_active())

    def test_cold_cache_is_incremental_and_warm_rebuild_has_no_per_advert_connections(self):
        rows, feature = self.rows(2000), PlaceFeature()
        with patch.object(place_feature, "LOCAL_BATCH_SIZE", 40):
            self.rebuild(feature, rows)
            self.assertEqual(len(store.read("SELECT key FROM place_advert_facts")), 40)
            self.rebuild(feature, rows)
            self.assertEqual(len(store.read("SELECT key FROM place_advert_facts")), 80)
        index = self.state / "index.sqlite3"
        statements = [(
            "INSERT OR REPLACE INTO place_advert_facts VALUES (?, ?, ?, ?, ?, 0, ?)",
            (row["key"], row["lat"], row["lng"], index.stat().st_mtime, time.time(), json.dumps(self.FACTS)))
            for row in rows]
        for start in range(0, len(statements), 200):
            self.assertTrue(store.write("", statements[start:start + 200]))
        self.rebuild(feature, rows)
        with patch.object(store, "read", wraps=store.read) as read, \
                patch.object(store, "write", wraps=store.write) as write, \
                patch.object(place, "local_osm_batch", side_effect=AssertionError("warm index lookup")):
            self.rebuild(feature, rows)
        self.assertLessEqual(read.call_count, 12)
        self.assertLessEqual(write.call_count, 3)
        child = PlaceFeature()
        with patch.object(child, "_rebuild", side_effect=AssertionError("rebuild in has_work")), \
                patch.object(place_feature, "_load_rows", side_effect=AssertionError("archive read in has_work")):
            from pisobot.optional.step import bounded
            ok, has = bounded(child.has_work, 5)
        self.assertTrue(ok and has)

    def test_after_cycle_only_marks_work_and_old_published_rows_expire(self):
        feature = PlaceFeature()
        with patch.object(store, "read", side_effect=AssertionError("cycle cache read")), \
                patch.object(store, "write", side_effect=AssertionError("cycle cache write")), \
                patch.object(feature, "_rebuild", side_effect=AssertionError("cycle rebuild")):
            self.assertIsNone(feature.after_cycle(()))
        self.rebuild(feature, self.rows(1))
        self.assertTrue(optional._snapshot)
        with patch.object(place_feature.time, "time", return_value=time.time() + 20 * 60):
            PlaceFeature().has_work()
            self.assertTrue(optional._snapshot)
        self.assertTrue(store.write("", [("UPDATE place_meta SET value = ? WHERE key = 'published_at'",
                                         (time.time() - place_feature.SNAPSHOT_MAX_AGE_SECONDS - 1,))]))
        feature.has_work()
        self.assertEqual(optional._snapshot, {})

    def test_missing_index_drops_old_lines_and_uses_a_short_retry(self):
        feature, rows = PlaceFeature(), self.rows(1)
        self.rebuild(feature, rows)
        with patch.object(place, "LOCAL_INDEX_PATH", str(self.state / "missing.sqlite3")), \
                patch.object(place_feature, "_load_rows", return_value=rows), \
                self.assertLogs(place_feature.log, "WARNING"):
            feature.step(time.monotonic() + 5)
        feature.has_work()
        self.assertEqual(optional._snapshot, {})
        retry = store.read("SELECT value FROM place_meta WHERE key = 'retry_at'")[0]["value"]
        self.assertLessEqual(retry - time.time(), place_feature.SNAPSHOT_REFRESH_SECONDS)

    def test_malformed_advert_and_card_cache_rows_do_not_stop_other_publishing(self):
        feature, rows = PlaceFeature(), self.rows(2)
        self.rebuild(feature, rows)
        for malformed in ('{"amenities":{"cafe":{"count":"wrong"}}}', "{}"):
            self.assertTrue(store.write("", [
                ("UPDATE place_advert_facts SET value = ? WHERE key = 'rm:0'", (malformed,)),
                ("UPDATE place_card_lines SET value = '[1]' WHERE key = 'rm:0'", ()),
            ]))
            with self.assertLogs(place_feature.log, "WARNING"):
                PlaceFeature().has_work()
                self.assertIn("rm:1", optional._snapshot)
                self.rebuild(feature, rows)
            self.assertEqual(len(optional._snapshot), 2)

    def test_index_batch_reuses_one_connection_and_deduplicates_shop_points_not_advert_distance(self):
        index = self.state / "index.sqlite3"
        self.make_index(index)
        db = sqlite3.connect(index)
        try:
            db.execute("UPDATE features SET lon = -0.097 WHERE id = 4")
            db.execute("INSERT INTO features SELECT 14,kind,class,name,'way/14',lat1,lon1,lat2,lon2,lat,lon+0.00001 "
                       "FROM features WHERE id = 4")
            db.execute("INSERT INTO grid_cells SELECT cell_x,cell_y,14 FROM grid_cells WHERE feature_id=4")
            db.commit()
        finally:
            db.close()
        with patch.object(place, "_readonly_db", wraps=place._readonly_db) as connect:
            results = place.local_osm_batch([("a", 51.5, -0.1), ("b", 51.5001, -0.1)],
                                            time.monotonic() + 5, index)
        self.assertEqual(connect.call_count, 1)
        self.assertEqual(results["a"]["amenities"]["supermarket"]["count"], 1)

    def test_index_budget_discards_partial_results(self):
        index = self.state / "index.sqlite3"
        self.make_index(index)
        with patch.object(place, "INDEX_MAX_ROWS", 1), self.assertLogs(place._logger, "WARNING"):
            result = place.local_osm_batch([("a", 51.5, -0.1)], time.monotonic() + 5, index)
        self.assertEqual(result, {"a": None})
        clock = [0]

        def deadline():
            clock[0] = 6
            raise TimeoutError("end of batch")

        with patch.object(place.time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(place, "local_osm_facts", side_effect=lambda *_a, **_k: deadline()):
            self.assertEqual(place.local_osm_batch([("a", 51.5, -0.1)], 5, index), {})

    def test_provider_wait_that_exceeds_step_budget_is_not_attempted_or_cached(self):
        place._ensure_cache()
        self.assertTrue(store.write("", [("INSERT INTO place_noise_state VALUES ('next_request_at', ?)",
                                         (time.time() + 30,))]))
        with patch.object(place.time, "sleep", side_effect=AssertionError("over-budget wait")), \
                self.assertRaises(place.RefreshDeferred):
            place._noise_point("https://fixture.test", "road", 51.5, -0.1, deadline=time.monotonic() + 2)
        self.assertEqual(store.read("SELECT * FROM place_noise_answers"), [])
        self.assertEqual(store.read("SELECT * FROM place_noise_failures"), [])
        self.assertEqual(store.read("SELECT * FROM place_request_claims"), [])

    def test_provider_deadline_is_checked_between_requests_within_one_cell(self):
        clock = [100.0]
        index = self.state / "index.sqlite3"
        self.make_index(index)

        class Response:
            status_code = 200
            text = "Results for FeatureType 'road':\nGRAY_INDEX = 42"

            def raise_for_status(self):
                return None

            def json(self):
                return {"stopPoints": []}

        def get(*_args, **kwargs):
            self.assertLess(clock[0], 107)
            self.assertLessEqual(kwargs["timeout"], (107 - clock[0]) / 2)
            clock[0] += 4
            return Response()

        with patch.object(place, "LOCAL_INDEX_PATH", str(index)), \
                patch.object(place.time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(place.time, "time", side_effect=lambda: clock[0] + 1000), \
                patch.object(place.requests, "get", side_effect=get) as request:
            with self.assertRaises(place.RefreshDeferred):
                place.refresh_network(51.5, -0.1, deadline=107)
        self.assertEqual(request.call_count, 2)

    def test_step_deadline_is_respected_before_and_after_the_archive_read(self):
        feature = PlaceFeature()
        with patch.object(place_feature, "_load_rows", side_effect=AssertionError("expired read")):
            feature.step(time.monotonic() - 1)
        clock = [100.0]

        def slow_read():
            clock[0] = 200.0
            return self.rows(1)

        with patch.object(place_feature.time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(place_feature, "_load_rows", side_effect=slow_read), \
                patch.object(place_feature, "_cache_rows", side_effect=AssertionError("expired cache read")):
            feature.step(101.0)

    def test_three_place_lines_preserve_real_telegram_send_plan_at_boundaries(self):
        row = self.rows(5)
        record = _area_record("N7", row, {item["key"]: self.FACTS for item in row},
                              {}, [30, 40, 50], time.time())
        long_names = {**self.FACTS, "amenities": {
            category: {"nearest": {"name": "<b>Very long & named 🏡 venue</b>" * 20, "distance_m": 300}}
            for category in ("supermarket", "convenience", "gym", "cinema")}}
        offers = [(100, place.amenities_line(long_names)),
                  (50, "🏘 Residential street · 2 rail/tube lines"),
                  (10, place_feature._area_line(record))]
        optional.publish_lines("place-facts", {"rm:1": offers})
        listing = types.SimpleNamespace(key="rm:1")
        with patch.object(place, "cached_facts", side_effect=AssertionError("first-alert cache read")), \
                patch.object(store, "read", side_effect=AssertionError("first-alert database read")):
            self.assertEqual(optional.extend_card("new card", types.SimpleNamespace(key="new:1"), True), "new card")
        for photo, base in ((True, "x" * 1010), (True, "🏡" * 508), (True, "<b>" + "x" * 1000 + "</b>"),
                            (False, "x" * 4090), (False, "🏡" * 2040), (False, "short card")):
            with self.subTest(photo=photo, size=len(base)):
                text = optional.extend_card(base, listing, photo)
                plans = []
                for content in (base, text):
                    with patch.object(telegram, "_call", return_value={"result": {"message_id": 1}}) as call, \
                            patch.object(telegram, "throttle"):
                        telegram.send_card(b"photo" if photo else None, content, None, "short")
                    plans.append([entry.args[0] for entry in call.call_args_list])
                self.assertEqual(plans[0], plans[1])
                self.assertEqual(optional.plan(photo, base), optional.plan(photo, text))


class PlaceCycleTests(JudgingBase):
    def test_two_person_cycle_and_later_sweep_are_not_delayed_by_a_blocked_optional_step(self):
        optional.reset()
        self.enterContext(patch.dict(os.environ, {"PLACE_FACTS": "1", "TELEGRAM_INVITE_USERNAMES": "fixture",
                                                 "HEALTHCHECK_URL": ""}))
        guest = users.register("200", "Fixture", "fixture")
        self.assertIsNotNone(guest)
        feature = PlaceFeature()
        optional.register("PLACE_FACTS", lambda: feature)
        events = []
        head = [advert(1, address="London N7")]
        self.enterContext(patch.object(rightmove, "fetch", side_effect=lambda: list(head)))
        self.enterContext(patch.object(sweep, "enabled", return_value=True))
        self.enterContext(patch.object(sweep, "step", side_effect=lambda *_a, **_k: events.append("sweep")))
        self.enterContext(patch.object(telegram, "send_listing", side_effect=lambda listing, reason:
                                      events.append((users.current_chat(), listing.key)) or
                                      {"result": {"message_id": 1}}))

        def stop_workers():
            workers = optional.workers()
            optional.reset()
            for worker in workers:
                worker.join(2)
                self.assertFalse(worker.is_alive())

        self.addCleanup(stop_workers)
        watcher = watch.Watcher(["rm"])
        watcher.cycle()
        events.clear()
        head.append(advert(2, address="London N7"))
        with patch.object(feature, "_rebuild", side_effect=AssertionError("cycle rebuilt optional facts")):
            started = time.monotonic()
            watcher.cycle()
            self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(events, [("100", "rm:2"), ("200", "rm:2"), "sweep"])
        for person in users.everyone():
            with users.use(person):
                self.assertIn("rm:2", main.load_seen())
            self.assertTrue(archive.verdicts_since(person.chat_id, 1))

        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        locks = []

        def delayed_read():
            with open(Path(self.tmp.name) / "run.lock", "a") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locks.append("free")
                except BlockingIOError:
                    locks.append("held")
            entered.set()
            release.wait(5)
            return []

        with patch.object(place_feature, "_load_rows", side_effect=delayed_read):
            job = threading.Thread(target=feature.step, args=(time.monotonic() + 0.05,))
            job.start()
            try:
                self.assertTrue(entered.wait(1))
                self.assertEqual(locks, ["free"])
                events.clear()
                head.append(advert(3, address="London N7"))
                started = time.monotonic()
                watcher.cycle()
                self.assertLess(time.monotonic() - started, 1.0)
                self.assertEqual(events, [("100", "rm:3"), ("200", "rm:3"), "sweep"])
                self.assertTrue(job.is_alive())
            finally:
                release.set()
                job.join(2)
        self.assertFalse(job.is_alive())


if __name__ == "__main__":
    unittest.main()
