"""All TfL traffic in these tests is fake; live builds are an explicit separate CLI."""
import contextlib
import datetime as dt
import io
import json
import math
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests

from pisobot import zone

FIXTURE = Path(__file__).with_name("fixtures_zone.json")
NOW = dt.datetime(2026, 11, 10, 9, tzinfo=dt.timezone.utc)


def fixture():
    return json.loads(FIXTURE.read_text())


def snapshot(stations=None, minutes=None):
    stations = stations or zone.normalise_stations(fixture())
    times = minutes or {s.id: 20 + i for i, s in enumerate(stations)}
    return {
        "schema": zone.SCHEMA, "destination": {"name": "test", "lat": 51.5152, "lon": -0.1419},
        "radius_km": 20, "started_at": NOW.isoformat(), "travel_date": "20261111",
        "stations": [s.to_dict() for s in stations], "station_table_errors": {},
        "results": {s.id: {"status": "ok", "minutes": times[s.id]} for s in stations},
    }


class FakeClient:
    def __init__(self):
        self.calls = self.failed = 0
        self.paths = []
        self.payloads = fixture()
        self.fail_once = set()

    def get(self, path, **params):
        self.calls += 1
        self.paths.append((path, params))
        if path in self.fail_once:
            self.fail_once.remove(path)
            self.failed += 1
            raise zone.TflError("http_503")
        if path.startswith("/StopPoint/"):
            return self.payloads[path.rsplit("/", 1)[-1]]
        return {"journeys": [{"duration": 31}, {"duration": 19}]}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.enterContext(patch.object(zone, "_now", return_value=NOW))
        # Fail loudly if fixture plumbing accidentally permits live HTTP.
        self.enterContext(patch.object(requests.sessions.Session, "request",
                                      side_effect=AssertionError("Unexpected live network in unit test")))


class StationTests(Base):
    def test_group_by_station_naptan_merge_modes_and_prefer_station_coordinates(self):
        stations = {s.id: s for s in zone.normalise_stations(fixture())}
        self.assertEqual(len(stations), 5)
        self.assertEqual(stations["station-a"], zone.Station(
            "station-a", "Alpha Underground Station", 51.5, -0.1, ("dlr", "tube")))
        self.assertAlmostEqual(stations["station-d"].lat, 51.5305)
        self.assertAlmostEqual(stations["station-d"].lon, -0.0805)
        self.assertEqual(stations["station-e"].modes, ("national-rail",))
        self.assertNotIn("bus", stations["station-a"].modes)
        self.assertEqual(zone.Station.from_dict(stations["station-a"].to_dict()), stations["station-a"])

    def test_bbox_clips_only_national_rail(self):
        data = fixture()
        data["tube"]["stopPoints"].append({"id": "far-tube", "commonName": "Far Tube",
                                           "lat": 51.8, "lon": -0.6, "modes": ["tube"]})
        data["tube"]["total"] += 1
        ids = {s.id for s in zone.normalise_stations(data)}
        self.assertIn("far-tube", ids)
        self.assertNotIn("far-away", ids)
        self.assertNotIn("west-of-box", ids)

    def test_missing_empty_and_truncated_mode_responses_are_not_silently_complete(self):
        for data in ({}, {"stopPoints": []}, {"total": 2, "stopPoints": [{}]}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                zone.normalise_stations({"tube": data})

    def test_one_call_per_mode_disk_resume_and_optional_national_rail_failure(self):
        client = FakeClient()
        client.fail_once.add("/StopPoint/Mode/national-rail")
        table = zone.station_table(client, self.directory)
        self.assertEqual(client.calls, 5)
        self.assertEqual(table["errors"], {"national-rail": "http_503"})
        table2 = zone.station_table(client, self.directory)
        self.assertEqual(client.calls, 5)
        self.assertEqual(table2["stations"], table["stations"])
        table3 = zone.station_table(client, self.directory, refresh=True)
        self.assertEqual(client.calls, 10)
        self.assertEqual(table3["errors"], {})
        self.assertEqual(len(table3["stations"]), 5)

    def test_required_mode_failure_keeps_successful_discovery_for_resume(self):
        client = FakeClient()
        client.fail_once.add("/StopPoint/Mode/dlr")
        with self.assertRaises(zone.TflError):
            zone.station_table(client, self.directory)
        zone.station_table(client, self.directory)
        self.assertEqual(sum(p == "/StopPoint/Mode/tube" for p, _ in client.paths), 1)


class ClientTests(Base):
    def test_pacing_all_calls_including_failures_key_in_request_not_errors(self):
        now = [0.0]
        sleeps = []
        def sleep(seconds):
            sleeps.append(seconds)
            now[0] += seconds
        session = Mock()
        session.get.side_effect = [Mock(status_code=200, json=lambda: {}),
                                   Mock(status_code=429),
                                   requests.Timeout("secret-key-in-url")]
        client = zone.TflClient(session=session, clock=lambda: now[0], sleep=sleep)
        with patch.dict(os.environ, {"TFL_APP_KEY": "secret-key-in-url"}):
            self.assertEqual(client.get("/StopPoint/Mode/tube"), {})
            with self.assertRaisesRegex(zone.TflError, "^http_429$"):
                client.get("/StopPoint/Mode/dlr")
            with self.assertRaisesRegex(zone.TflError, "^transport_or_json_error$") as error:
                client.get("/StopPoint/Mode/overground")
        self.assertNotIn("secret-key", str(error.exception))
        self.assertEqual((client.calls, client.failed), (3, 2))
        self.assertEqual(len(sleeps), 2)
        self.assertTrue(all(n > 1.5 for n in sleeps))
        self.assertEqual(session.get.call_args.kwargs["params"]["app_key"], "secret-key-in-url")
        self.assertFalse(session.get.call_args.kwargs["allow_redirects"])
        for rate in (0, -1, 41, float("nan")):
            with self.assertRaises(ValueError):
                zone.TflClient(rpm=rate)
        with self.assertRaises(ValueError):
            client.get("https://other.example/secret")

    def test_non_object_and_invalid_json_fail_safely(self):
        for response in (Mock(status_code=200, json=lambda: []),
                         Mock(status_code=200, json=Mock(side_effect=ValueError("unsafe")))):
            client = zone.TflClient(session=Mock(get=Mock(return_value=response)))
            with self.assertRaises(zone.TflError):
                client.get("/StopPoint/Mode/tube")
            self.assertEqual(client.failed, 1)


class BuildTests(Base):
    def test_checkpoint_resume_minimum_journey_and_date_parameters(self):
        client = FakeClient()
        failed_path = "/Journey/JourneyResults/51.50000,-0.10000/to/51.5152,-0.1419"
        client.fail_once.add(failed_path)
        first = zone.build(51.5152, -0.1419, "Oxford Circus", data_dir=self.directory,
                           client=client, progress=lambda _: None)
        self.assertFalse(first.complete)
        self.assertEqual(first.data["results"]["station-a"]["status"], "error")
        self.assertEqual(len(first.stations), 5)
        self.assertEqual(client.calls, 10)
        second = zone.build(51.5152, -0.1419, "Oxford Circus", data_dir=self.directory,
                            client=client, progress=lambda _: None)
        self.assertTrue(second.complete)
        self.assertEqual(client.calls, 11)  # only failed station retried
        self.assertEqual(second.data["results"]["station-a"]["minutes"], 19)
        self.assertEqual(second.data["runs"][-1]["discovery_calls"], 0)
        self.assertEqual(second.data["runs"][-1]["calls"], 1)
        params = client.paths[-1][1]
        self.assertEqual(params["time"], "0830")
        self.assertEqual(params["timeIs"], "Departing")
        self.assertRegex(params["date"], r"^\d{8}$")
        # A third invocation performs no network, not even station discovery.
        zone.build(51.5152, -0.1419, "Oxford Circus", data_dir=self.directory,
                   client=client, progress=lambda _: None)
        self.assertEqual(client.calls, 11)
        self.assertEqual(zone.load(self.directory / "oxford-circus.json").lat, 51.5152)
        with self.assertRaises(ValueError):
            zone.build(51.52, -0.075, "Oxford Circus", data_dir=self.directory,
                       client=client, progress=lambda _: None)

    def test_separate_destination_files_and_twenty_km_radius(self):
        client = FakeClient()
        zone.build(51.5152, -0.1419, "Oxford", data_dir=self.directory,
                   client=client, progress=lambda _: None)
        shoreditch = zone.build(51.523375, -0.075246, "Shoreditch", data_dir=self.directory,
                               client=client, radius_km=1, progress=lambda _: None)
        self.assertTrue((self.directory / "oxford.json").exists())
        self.assertTrue((self.directory / "shoreditch.json").exists())
        self.assertTrue(all(zone.distance_km(s.lat, s.lon, shoreditch.lat, shoreditch.lon) <= 1
                            for s in shoreditch.stations))
        self.assertEqual(client.calls, 10 + len(shoreditch.stations))

    def test_no_route_is_cached_but_bad_schema_is_retried(self):
        client = FakeClient()
        original_get = client.get
        def get(path, **params):
            reply = original_get(path, **params)
            if path.startswith("/Journey/"):
                return {"journeys": []}
            return reply
        client.get = get
        data = zone.build(51.5152, -0.1419, "test", data_dir=self.directory,
                          client=client, progress=lambda _: None)
        self.assertTrue(data.complete)
        self.assertTrue(all(v["status"] == "no_route" for v in data.data["results"].values()))
        self.assertIsNone(zone.estimate(51.5, -0.1, data))
        zone.build(51.5152, -0.1419, "test", data_dir=self.directory,
                   client=client, progress=lambda _: None)
        self.assertEqual(client.calls, 10)

    def test_malformed_journey_results_are_retryable_not_permanent_no_route(self):
        client = FakeClient()
        original_get = client.get
        for reply in ({}, {"journeys": [None]}, {"journeys": [{"duration": -1}]},
                      {"journeys": [{"duration": float("nan")}]},
                      {"journeys": [{"not_duration": 10}]}):
            with self.subTest(reply=reply):
                def get(path, **params):
                    value = original_get(path, **params)
                    return reply if path.startswith("/Journey/") else value
                client.get = get
                data = zone.build(51.5152, -0.1419, "bad", data_dir=self.directory,
                                  client=client, progress=lambda _: None)
                self.assertEqual(data.data["runs"][-1]["failed"], 5)
                self.assertTrue(all(v["status"] == "error" for v in data.data["results"].values()))
                self.assertFalse(data.complete)
        client.get = original_get
        recovered = zone.build(51.5152, -0.1419, "bad", data_dir=self.directory,
                               client=client, progress=lambda _: None)
        self.assertTrue(recovered.complete)

    def test_interruption_preserves_previous_station_and_resumes(self):
        client = FakeClient()
        original = client.get
        journey_calls = [0]
        def interrupt(path, **params):
            if path.startswith("/Journey/"):
                journey_calls[0] += 1
                if journey_calls[0] == 2:
                    raise KeyboardInterrupt
            return original(path, **params)
        client.get = interrupt
        with self.assertRaises(KeyboardInterrupt):
            zone.build(51.5152, -0.1419, "test", data_dir=self.directory,
                       client=client, progress=lambda _: None)
        self.assertEqual(len(zone._read(self.directory / "test.json")["results"]), 1)
        client.get = original
        zone.build(51.5152, -0.1419, "test", data_dir=self.directory,
                   client=client, progress=lambda _: None)
        self.assertEqual(client.calls, 10)

    def test_monthly_expiry_rebuilds_journeys_and_next_weekday_skips_weekend(self):
        self.assertEqual(zone.next_weekday(dt.date(2026, 11, 13)), "20261116")
        self.assertEqual(zone.next_weekday(dt.date(2026, 11, 14)), "20261116")
        self.assertEqual(zone.next_weekday(dt.date(2026, 11, 15)), "20261116")
        client = FakeClient()
        zone.build(51.5152, -0.1419, "test", data_dir=self.directory,
                   client=client, progress=lambda _: None)
        with patch.object(zone, "_now", return_value=NOW + dt.timedelta(days=31)):
            zone.build(51.5152, -0.1419, "test", data_dir=self.directory,
                       client=client, progress=lambda _: None)
        self.assertEqual(client.calls, 20)
        self.assertFalse(list(self.directory.glob(".zone-*")))


class EstimateTests(Base):
    def test_best_of_nearest_stations_includes_walking_not_just_closest(self):
        stations = [zone.Station("a", "A", 51.5, -0.1, ("tube",)),
                    zone.Station("b", "B", 51.501, -0.1, ("tube",))]
        data = zone.ZoneData(snapshot(stations, {"a": 30, "b": 10}))
        result = zone.estimate(51.5, -0.1, data)
        self.assertEqual(result.station.id, "b")
        self.assertAlmostEqual(result.walk_minutes, zone.walk_minutes(zone.distance_km(51.5, -0.1, 51.501, -0.1)))
        self.assertAlmostEqual(result.minutes, result.walk_minutes + 10)
        self.assertAlmostEqual(zone.walk_minutes(1), 15.625)
        self.assertAlmostEqual(zone.distance_km(0, 0, 0, 1), 111.195, places=2)
        self.assertTrue(zone.could_reach(51.5, -0.1, data, result.minutes, 0))
        self.assertFalse(zone.could_reach(51.5, -0.1, data, result.minutes - 0.1, 0))
        self.assertTrue(zone.could_reach(51.5, -0.1, data, result.minutes - 0.1, 0.2))

    def test_only_nearest_five_groups_considered(self):
        stations = [zone.Station(str(i), str(i), 51.5 + i * 0.001, -0.1, ("tube",)) for i in range(6)]
        times = {s.id: 50 for s in stations}
        times["5"] = 0
        result = zone.estimate(51.5, -0.1, zone.ZoneData(snapshot(stations, times)))
        self.assertEqual(result.station.id, "0")
        self.assertEqual(result.minutes, 50)

    def test_unknown_stale_missing_and_outside_radius_fail_open(self):
        data = snapshot()
        data["results"]["station-a"] = {"status": "error"}
        self.assertIsNone(zone.estimate(51.5, -0.1, zone.ZoneData(data)))
        self.assertTrue(zone.could_reach(51.5, -0.1, zone.ZoneData(data), 0, 0))
        data["started_at"] = (NOW - dt.timedelta(days=31)).isoformat()
        self.assertTrue(zone.could_reach(51.5, -0.1, zone.ZoneData(data), 0, 0))
        self.assertTrue(zone.could_reach(53, 0, zone.ZoneData(snapshot()), 0, 0))
        self.assertTrue(zone.could_reach(51.5, -0.1, self.directory / "missing.json"))
        for lat, lon in ((math.nan, 0), (91, 0), (51, 181)):
            with self.assertRaises(ValueError):
                zone.estimate(lat, lon, zone.ZoneData(snapshot()))
        for limit, margin in ((-1, 5), (40, -1), (40, math.inf)):
            with self.assertRaises(ValueError):
                zone.could_reach(51.5, -0.1, zone.ZoneData(snapshot()), limit, margin)

    def test_portal_circle_encloses_remaining_walk_allowance_and_empty_zone(self):
        data = zone.ZoneData(snapshot())
        shape = zone.portal_zone(data, 40, 5)
        self.assertEqual(len(shape.stations), 5)
        self.assertTrue(shape.complete)
        for station in shape.stations:
            access_km = (45 - data.data["results"][station.id]["minutes"]) * zone.WALK_KMH / 60 / zone.WALK_DETOUR
            self.assertLessEqual(zone.distance_km(*shape.centre, station.lat, station.lon) + access_km,
                                 shape.radius_km + 1e-10)
        empty = zone.portal_zone(data, 0, 0)
        self.assertEqual(empty.stations, frozenset())
        self.assertIsNone(empty.centre)
        self.assertIsNone(empty.radius_km)


    def test_missing_optional_mode_is_visible_and_portal_envelope_is_not_complete(self):
        data = snapshot()
        data["station_table_errors"] = {"national-rail": "http_503"}
        parsed = zone.ZoneData(data)
        self.assertTrue(parsed.complete)  # all discovered station journeys completed
        shape = zone.portal_zone(parsed)
        self.assertFalse(shape.complete)  # but not full transport-mode coverage
        self.assertEqual(shape.missing_modes, ("national-rail",))

    def test_remote_station_failure_does_not_disable_entire_prefilter(self):
        stations = [zone.Station(str(i), str(i), 51.5 + i * 0.001, -0.1, ("tube",)) for i in range(6)]
        data = snapshot(stations, {s.id: 50 for s in stations})
        data["results"]["5"] = {"status": "error"}
        parsed = zone.ZoneData(data)
        self.assertFalse(parsed.complete)
        self.assertFalse(zone.could_reach(51.5, -0.1, parsed, 40, 0))
        self.assertTrue(zone.could_reach(stations[-1].lat, stations[-1].lon, parsed, 40, 0))


class ValidationTests(Base):
    def test_exact_cache_duplicates_legacy_qualified_other_dest_and_empty(self):
        points, duplicates, skipped = zone.exact_points({"journey": {
            "51.5,-0.1": {"minutes": 50},
            "51.5,-0.1->51.5152,-0.1419": {"minutes": 40},
            "51.51,-0.1": {"minutes": 35},
            "51.52,-0.1->51.523375,-0.075246": {"minutes": 20},
            "51.53,-0.1": {}, "invalid": {},
        }})
        self.assertEqual(points, {(51.5, -0.1): 40, (51.51, -0.1): 35})
        self.assertEqual((duplicates, skipped), (1, 3))

    def test_validate_reports_real_errors_rates_and_never_writes_cache(self):
        data = snapshot()
        path = self.directory / "test.json"
        zone._write(path, data)
        cache_path = self.directory / "cache.json"
        zone._write(cache_path, {"journey": {
            "51.5,-0.1": {"minutes": 40}, "51.51,-0.1": {"minutes": 60}}})
        original = cache_path.read_bytes()
        lines = []
        rows = zone.validate(path, cache_path, output=lines.append)
        self.assertEqual(cache_path.read_bytes(), original)
        self.assertEqual(len(rows), 2)
        text = "\n".join(lines)
        self.assertIn("Mean absolute error:", text)
        self.assertIn("90th-percentile absolute error (nearest rank):", text)
        self.assertIn("Limit 40, margin  0:", text)
        self.assertIn("Limit 40, margin  5:", text)
        self.assertIn("Limit 40, margin 10:", text)
        self.assertIn("NOT MET", text)
        self.assertIn("No constants fitted", text)

    def test_unqualified_destinations_are_flagged_and_explicit_subset_also_reported(self):
        path = self.directory / "test.json"
        cache_path = self.directory / "cache.json"
        zone._write(path, snapshot())
        zone._write(cache_path, {"journey": {
            "51.5,-0.1": {"minutes": 15, "route": "Overground (Windrush)"},
            "51.51,-0.1->51.5152,-0.1419": {"minutes": 35},
        }})
        lines = []
        rows = zone.validate(path, cache_path, output=lines.append)
        self.assertEqual(len(rows), 2)  # requested comparison still includes legacy
        text = "\n".join(lines)
        self.assertIn("1 legacy entries have no destination; 1 end", text)
        self.assertIn("Explicit-destination-only comparison (no model/constants changed)", text)
        self.assertIn("Compared: 1 estimates;", text)
        self.assertEqual(text.count("Mean absolute error:"), 2)

    def test_validate_empty_cache_has_no_division_by_zero_or_false_target_success(self):
        path = self.directory / "test.json"
        cache_path = self.directory / "empty.json"
        zone._write(path, snapshot())
        zone._write(cache_path, {"journey": {}})
        lines = []
        self.assertEqual(zone.validate(path, cache_path, output=lines.append), [])
        self.assertIn("N/A (no estimates)", "\n".join(lines))
        self.assertIn("NOT MET", "\n".join(lines))

    def test_cli_build_and_validate_arguments_without_network(self):
        with patch.object(zone, "build") as build:
            zone.main(["build", "--dest", "51.5152,-0.1419", "--name", "Oxford Circus",
                       "--data-dir", str(self.directory)])
        self.assertEqual(build.call_args.args, (51.5152, -0.1419, "Oxford Circus"))
        with patch.object(zone, "validate") as validate:
            zone.main(["validate"])
        validate.assert_called_once_with("oxford-circus", None)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            zone.main(["build", "--dest", "not-coordinates", "--name", "test"])
        self.assertEqual(error.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
