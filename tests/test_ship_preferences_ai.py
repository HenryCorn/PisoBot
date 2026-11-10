"""Offline regressions for shipping preferences + AI on the deployed hotfix base."""
import os
import unittest
from unittest.mock import Mock, patch

from pisobot import cache, config, enrich, extract, main, rental_text, telegram
from test_extract import FakeClient, listing, record


class ShippingSeams(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(cache, "_data", {}))
        self.enterContext(patch.dict(os.environ, {"STORAGE": "local", "ANTHROPIC_API_KEY": ""}))
        for name, value in (("STUDIO_MIN_AREA", None), ("FURNISHED", "any"),
                            ("MAX_WALK_MINUTES", None), ("MAX_CHANGES", None),
                            ("AREAS_INCLUDE", ()), ("AREAS_EXCLUDE", ())):
            self.enterContext(patch.object(config, name, value))
        self.addCleanup(extract.set_default, None)

    def test_shared_room_is_not_a_studio_without_an_actual_studio_mention(self):
        self.assertFalse(rental_text.is_studio("Room in a shared house"))
        self.assertFalse(rental_text.is_studio("Shared kitchen and bathroom"))
        self.assertTrue(rental_text.is_studio("Modern studio flat"))

    def test_historical_and_negated_studio_mentions_do_not_create_studio_dealbreakers(self):
        for text in ("Once a studio flat, now a separate bedroom", "Formerly a studio flat",
                     "Previously a studio apartment", "Not a studio flat"):
            with self.subTest(text=text):
                self.assertFalse(rental_text.is_studio(text))
        self.assertTrue(rental_text.is_studio("Previously a studio flat. Now a modern studio apartment."))

    def test_ai_cannot_override_a_real_studio_opt_out(self):
        client = FakeClient(record("whole_flat", "high", "Entire flat"))
        self.enterContext(patch.object(extract, "keyword_verdict", side_effect=AssertionError("no paid call")))
        self.assertEqual(main.passes_basic(listing("Bright studio flat with kitchenette")), "studio")
        self.assertEqual(client.calls, [])

    def test_furnishing_and_studio_area_gates_precede_ai_calls(self):
        self.enterContext(patch.object(main.SOURCES["rm"], "refine", return_value=True))
        with patch.object(extract, "keyword_verdict", side_effect=AssertionError("no paid call")):
            with patch.object(config, "FURNISHED", "unfurnished"):
                self.assertEqual(main.passes_basic(listing("Bright furnished flat", furnishing="furnished")),
                                 "furnishing")
            with patch.object(config, "STUDIO_MIN_AREA", 35):
                self.assertEqual(main.passes_basic(listing("Studio flat", bedrooms=0, floor_area_m2=25)),
                                 "studio_area")

    def test_details_reclassify_studio_before_size_and_ai_checks(self):
        def refine(home):
            home.description += " Studio apartment."
            return True
        with patch.object(main.SOURCES["rm"], "refine", side_effect=refine), \
                patch.object(extract, "keyword_verdict", return_value=False) as model:
            with patch.object(config, "STUDIO_MIN_AREA", 28):
                for area, expected in ((None, "studio_area"), (22, "studio_area"), (28, None)):
                    with self.subTest(area=area):
                        home = listing("Compact flat.", floor_area_m2=area)
                        self.assertEqual(main.passes_basic(home), expected)
                        self.assertTrue(home.studio)
                self.assertEqual(model.call_count, 1)  # size failures spend no model calls
            self.assertEqual(main.passes_basic(listing("Compact flat.")), "studio")

    def test_furnishing_is_property_specific_and_respects_negation(self):
        cases = {"This flat is not furnished.": "unfurnished", "The flat isn't furnished.": "unfurnished",
                 "Bright flat next to a furnished show apartment.": None,
                 "Part furnished flat": "part", "Furnished or unfurnished": "flexible",
                 "Furnished apartment": "furnished", "Unfurnished flat": "unfurnished",
                 "The flat is not unfurnished": None}
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(rental_text.furnishing(text), expected)
        with patch.object(main.SOURCES["rm"], "refine", return_value=True):
            with patch.object(config, "FURNISHED", "furnished"):
                self.assertEqual(main.passes_basic(listing("This flat is not furnished.")), "furnishing")
            with patch.object(config, "FURNISHED", "unfurnished"):
                self.assertIsNone(main.passes_basic(listing("Bright flat next to a furnished show apartment.")))

    def response(self):
        def option(minutes, walk, rides):
            legs = [{"duration": walk, "mode": {"id": "walking"}}]
            legs += [{"duration": 5, "mode": {"id": "tube"},
                      "routeOptions": [{"name": f"Line {i}"}],
                      "departurePoint": {"commonName": "Test Station"}} for i in range(rides)]
            return {"duration": minutes, "legs": legs}
        return Mock(json=Mock(return_value={"journeys": [option(25, 18, 3), option(32, 5, 1)]}))

    def test_slower_feasible_journey_is_selected_without_cross_person_cache_leak(self):
        with patch.object(enrich.net, "get", return_value=self.response()) as fetch:
            self.assertEqual(enrich.journey(51.52, -.14)["minutes"], 25)
            with patch.object(config, "MAX_WALK_MINUTES", 10), patch.object(config, "MAX_CHANGES", 0):
                chosen = enrich.journey(51.52, -.14)
                self.assertEqual((chosen["minutes"], chosen["walk"], chosen["changes"]), (32, 5, 0))
            self.assertEqual(enrich.journey(51.52, -.14)["minutes"], 25)
            fetch.assert_called_once()

    def test_legacy_fastest_cache_does_not_hide_other_feasible_options(self):
        key = f"51.520,-0.140->{config.TARGET_LAT},{config.TARGET_LNG}"
        cache.put("journey", key, {"minutes": 25, "walk": 18, "changes": 2})
        with patch.object(config, "MAX_CHANGES", 0), \
                patch.object(enrich.net, "get", return_value=self.response()) as fetch:
            self.assertEqual(enrich.journey(51.52, -.14)["minutes"], 32)
            self.assertEqual(enrich.journey(51.52, -.14)["minutes"], 32)
            fetch.assert_called_once()

    def test_both_constraints_apply_and_no_match_preserves_rejection_diagnostics(self):
        with patch.object(config, "MAX_WALK_MINUTES", 1), patch.object(config, "MAX_CHANGES", 0), \
                patch.object(enrich.net, "get", return_value=self.response()):
            self.assertEqual(enrich.journey(51.52, -.14)["minutes"], 25)
            self.assertEqual(main.passes_area(listing()), "walk")
