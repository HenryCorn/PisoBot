import unittest
from unittest.mock import patch

from pisobot import cache, place
from pisobot.models import Listing
from pisobot.telegram import format_listing


class PlaceFactsTests(unittest.TestCase):
    def test_osm_parser_counts_amenities_and_classifies_nearby_roads(self):
        data = {"elements": [
            {"type": "way", "id": 1, "center": {"lat": 51.5, "lon": -0.1},
             "tags": {"highway": "residential"}},
            {"type": "way", "id": 2,
             "geometry": [{"lat": 51.499, "lon": -0.1}, {"lat": 51.501, "lon": -0.1}],
             "tags": {"highway": "primary"}},
            {"type": "way", "id": 3,
             "geometry": [{"lat": 51.5, "lon": -0.098}, {"lat": 51.501, "lon": -0.098}],
             "tags": {"railway": "rail"}},
            {"type": "node", "id": 4, "lat": 51.5005, "lon": -0.1,
             "tags": {"shop": "supermarket", "name": "Lidl"}},
            {"type": "way", "id": 5, "center": {"lat": 51.501, "lon": -0.1},
             "tags": {"leisure": "park", "name": "Community Park"}},
            {"type": "way", "id": 7, "center": {"lat": 51.50101, "lon": -0.1},
             "tags": {"leisure": "park", "name": "Community Park"}},
            {"type": "node", "id": 6, "lat": 51.502, "lon": -0.1,
             "tags": {"amenity": "cafe"}},
        ]}
        facts = place.parse_overpass(data, 51.5, -0.1)
        self.assertEqual(facts["street"]["class"], "residential")
        self.assertEqual(facts["amenities"]["supermarket"]["nearest"]["name"], "Lidl")
        self.assertEqual(facts["amenities"]["park"]["count"], 1)
        self.assertEqual(place.street_label(facts), "On a main road")

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
        osm = {"elements": [{"type": "way", "id": 1, "center": {"lat": 51.5, "lon": -0.1},
                             "tags": {"highway": "residential"}}]}
        with patch.object(place, "_overpass", return_value=osm) as overpass, \
                patch.object(place, "_tfl", return_value={"stopPoints": []}) as tfl, \
                patch.object(place, "_noise", return_value={"road_lnight_db": 42.0,
                                                            "rail_lnight_db": None}) as noise:
            first = place.facts(51.5, -0.1, now=1000)
            second = place.facts(51.5001, -0.1001, now=1001)
            self.assertEqual(first, second)
            overpass.assert_called_once()
            tfl.assert_called_once()
            noise.assert_called_once()
            place.facts(51.5, -0.1, now=1000 + place.CACHE_TTL_SECONDS + 1)
            self.assertEqual(overpass.call_count, 2)

    def test_defra_point_value_parser_ignores_zero_and_errors(self):
        self.assertEqual(place._noise_value("GRAY_INDEX = 42.5"), 42.5)
        self.assertIsNone(place._noise_value("GRAY_INDEX = 0.0"))
        self.assertIsNone(place._noise_value("No features found"))

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
        self.assertIn("🏘 Quiet residential street · 2 lines + 9 bus routes · Lnight road 42 dB · Lidl ~250 m",
                      rendered)
        listing.place_facts = {}
        self.assertNotIn("🏘", format_listing(listing))


if __name__ == "__main__":
    unittest.main()
