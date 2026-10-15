import datetime as dt
import json
import unittest
from unittest.mock import Mock, patch

from pisobot import cache, config, enrich
from pisobot.models import Listing
from pisobot.sources import detail_fields, lease_remaining, lq, onthemarket, sharetobuy


class DetailTests(unittest.TestCase):
    def test_monthly_annual_and_weekly_costs_preserve_pence(self):
        self.assertEqual(detail_fields("Rent £464.06pcm; Service charge £3,600 per annum"),
                         {"rent": 464.06, "service_charge": 300})
        self.assertEqual(detail_fields("£50 pw rent; £250.01 monthly service charge"),
                         {"rent": 50 * 52 / 12, "service_charge": 250.01})
        self.assertEqual(detail_fields("Service charge: £3,000/year"), {"service_charge": 250})
        self.assertEqual(detail_fields("Annual service charge: £3,600"), {"service_charge": 300})
        self.assertEqual(detail_fields("Service charge (per annum): £3,600. Rent (pcm): £481"),
                         {"service_charge": 300, "rent": 481})
        self.assertEqual(detail_fields("Rent £120pw; service charge £3600pa"),
                         {"rent": 520, "service_charge": 300})

    def test_remaining_lease_and_total_area_not_room_dimensions(self):
        self.assertEqual(detail_fields("Lease: 89 years; Total internal floor area: 51 m²"),
                         {"lease_years": 89, "floor_area_m2": 51})
        self.assertAlmostEqual(detail_fields("Size: 538 sq ft")["floor_area_m2"], 49.98)
        self.assertEqual(detail_fields("Living room: 25 m². Balcony: 8 m²."), {})
        self.assertEqual(lease_remaining("999 years from and including 12th January 2023"),
                         999 - (dt.date.today().year - 2023))
        self.assertIsNone(lease_remaining("TBC"))
        self.assertEqual(detail_fields("Leasehold: 125 year lease from 1990")["lease_years"],
                         125 - (dt.date.today().year - 1990))
        self.assertEqual(detail_fields("Lease: 99 years commencing 1st April 1980")["lease_years"],
                         99 - (dt.date.today().year - 1980))

    def test_warning_thresholds_and_full_market_area_value(self):
        l = Listing("stb", "1", "https://example.com", "Home", 1, "SO",
                    service_charge=250, lease_years=90, full_value=300000, floor_area_m2=50)
        self.assertEqual(l.warnings, [])
        self.assertEqual(l.price_per_m2, 6000)
        l.service_charge, l.lease_years = 250.01, 89
        self.assertIn("High SC", l.warnings)
        self.assertIn("short lease 89 yrs", l.warnings)

    def test_lq_floorplan_and_total_area_are_scoped_to_this_property(self):
        html = ('<main><h1>Plot 2</h1><section><div class="c-floorplan-slider">'
                '<figure data-bg="https://example.com/Type-03a-Plot-2.png"></figure></section>'
                '<table><tr><td>Total</td><td>52.6 sq m</td><td>566.3 sq ft</td></tr></table></main>'
                '<footer>Floor area: 999 m²</footer>')
        d = lq._page_details(html)
        self.assertEqual(d["floor_area_m2"], 52.6)
        self.assertEqual(d["floorplan"], "https://example.com/Type-03a-Plot-2.png")

    def test_lq_detail_cache_separates_same_plot_id_on_different_developments(self):
        pages = ['<main><section><div class="c-floorplan-slider">'
                 f'<figure data-bg="https://example.com/{name}.png"></figure></section></main>'
                 for name in ("first", "second")]
        listings = [Listing("lq", "1", f"https://example.com/{name}/plot-1", name, 1, "SO")
                    for name in ("first", "second")]
        with patch.object(cache, "_data", {}), \
                patch.object(lq.net, "get", side_effect=[Mock(text=page) for page in pages]) as get:
            for l in listings:
                self.assertTrue(lq.refine(l))
        self.assertEqual(get.call_count, 2)
        self.assertNotEqual(listings[0].floorplan, listings[1].floorplan)

    def test_onthemarket_uses_structured_plan_area_and_full_advert_costs(self):
        p = {"id": 1, "minimumAreaSqM": 50, "floorplans": [{"largeUrl": "https://example.com/plan.png"}],
             "description": "25% share. Full market value: £270,000. Rent £464.06pcm. "
                            "Annual service charge £3,600. Lease: 125 years from 2004."}
        html = '<script id="__NEXT_DATA__">' + json.dumps(
            {"props": {"pageProps": {"initialReduxState": {"property": p}}}}) + "</script>"
        l = Listing("otm", "1", "https://example.com/1", "Home", 1, "SO")
        with patch.object(cache, "_data", {}), patch.object(onthemarket.net, "get", return_value=Mock(text=html)):
            self.assertTrue(onthemarket.refine(l))
        self.assertEqual((l.rent, l.service_charge, l.floor_area_m2), (464.06, 300, 50))
        self.assertEqual(l.lease_years, 125 - (dt.date.today().year - 2004))
        self.assertEqual(l.floorplan, "https://example.com/plan.png")

    def test_sharetobuy_prefers_precise_advert_costs_over_rounded_calculator(self):
        html = ('<div id="property-1">£464 rent £248 service charge '
                '<div id="description-content"><p>25% share. Rent £464.06pcm. '
                'Service charges: £248.11pcm. Lease: 990 years.</p></div>'
                '<div class="property-information-and-downloads">'
                '<a href="https://example.com/plan.jpg?download=1" class="download-floor-plan">Floorplan</a></div>'
                '<footer>Total area: 999 m²</footer>')
        with patch.object(cache, "_data", {}), patch.object(sharetobuy.net, "get", return_value=Mock(text=html)):
            d = sharetobuy._details("1", 1100)
        self.assertEqual((d["rent"], d["service_charge"]), (464.06, 248.11))
        self.assertEqual(d["floorplan"], "https://example.com/plan.jpg?download=1")
        self.assertIsNone(d["floor_area_m2"])

    def test_journey_cache_includes_destination_and_preserves_first_mile_walk(self):
        legs = [{"mode": {"id": "walking"}, "duration": 5},
                {"mode": {"id": "walking"}, "duration": 7},
                {"mode": {"id": "elizabeth-line"}, "duration": 19,
                 "departurePoint": {"commonName": "Custom House Rail Station"}}]
        response = Mock()
        response.json.return_value = {"journeys": [{"duration": 31, "legs": legs}]}
        with patch.object(cache, "_data", {}), patch.object(enrich.net, "get", return_value=response) as get:
            first = enrich.journey(51.5, -0.1)
            self.assertEqual((first["walk"], first["station"]), (12, "Custom House"))
            enrich.journey(51.5, -0.1)
            self.assertEqual(get.call_count, 1)
            with patch.object(config, "TARGET_LAT", 51.5233):
                enrich.journey(51.5, -0.1)
            self.assertEqual(get.call_count, 2)
