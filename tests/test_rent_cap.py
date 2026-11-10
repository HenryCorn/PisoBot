"""Advertised rent is the hard limit; bills are estimated from the council tax band and shown."""
import json
import unittest
from unittest.mock import patch

from pisobot import cache, config, costs, council_tax, enrich, intelligence, main, prefs, ranking, telegram, users
from pisobot.models import Listing
from pisobot.sources import PRIVATE_RENT, private_rent_costs, rental_in_scope, rightmove


def flat(rent=1550, **kw):
    values = dict(source="rm", id="1", url="u", address="Elderfield Road, Hackney, London E5", bedrooms=1,
                  scheme=PRIVATE_RENT, tenure="rent", rent=rent, lat=51.51, lng=-0.14)
    values.update(kw)
    return private_rent_costs(Listing(**values))


class CouncilTaxTests(unittest.TestCase):
    def test_every_london_billing_authority_is_listed_with_its_source(self):
        self.assertEqual(len(council_tax.BAND_D), 33)       # 32 boroughs + City of London
        self.assertEqual(council_tax.YEAR, "2026-27")
        self.assertIn("MHCLG", council_tax.SOURCE)
        self.assertEqual(council_tax.BAND_D["Hackney"], 2060.3)
        self.assertEqual(council_tax.LONDON_BAND_D, 2068.47)

    def test_band_ratios_follow_the_statutory_scale(self):
        ratios = council_tax.BAND_RATIO
        self.assertEqual([round(ratios[b] * 9, 6) for b in "ABCDEFGH"], [6, 7, 8, 9, 11, 13, 15, 18])

    def test_known_band_and_borough_is_exact(self):
        amount, label, exact = council_tax.monthly("b", "Hackney")
        self.assertEqual((amount, label, exact), (round(2060.3 * 7 / 9 / 12), "council tax B", True))

    def test_unknown_borough_or_band_falls_back_and_says_so(self):
        self.assertEqual(council_tax.monthly("D", None),
                         (round(2068.47 / 12), "council tax D, London average", False))
        self.assertEqual(council_tax.monthly(None, "Hackney"),
                         (round(2060.3 * 8 / 9 / 12), "council tax ~C, London average", False))
        amount, label, exact = council_tax.monthly(None, None)
        self.assertEqual((amount, label, exact), (round(2068.47 * 8 / 9 / 12), "council tax ~C, London average", False))

    def test_borough_comes_from_a_whole_address_part_or_outcode_never_a_street_name(self):
        self.assertEqual(council_tax.borough("Elderfield Road, Hackney, London E5", ""), "Hackney")
        self.assertEqual(council_tax.borough("1 Foo St, Fulham, London", ""), "Hammersmith and Fulham")
        self.assertEqual(council_tax.borough("Camden Road, London N7", "N7"), "Islington")   # not Camden
        self.assertIsNone(council_tax.borough("Westminster Bridge Road, London", "SE1X"))
        self.assertIsNone(council_tax.borough("Somewhere, London", "ZZ9"))

    def test_band_is_read_from_advert_text(self):
        self.assertEqual(council_tax.band_from_text("Council Tax Band: d. EPC C"), "D")
        self.assertEqual(council_tax.band_from_text("council tax band is F"), "F")
        self.assertIsNone(council_tax.band_from_text("no council tax mentioned"))


class BillsEstimateTests(unittest.TestCase):
    def test_advert_band_and_borough_give_an_unlabelled_exact_council_tax(self):
        l = flat(council_tax_band="B")
        tax = round(2060.3 * 7 / 9 / 12)
        self.assertEqual((l.council_tax_monthly, l.utilities_monthly, l.bills),
                         (tax, config.RENT_BILLS_ESTIMATE, tax + config.RENT_BILLS_ESTIMATE))
        self.assertEqual((l.council_tax_label, l.estimated), ("council tax B", ["bills"]))

    def test_unknown_band_uses_labelled_london_band_c_and_flags_the_estimate(self):
        l = flat(address="Somewhere, London", bills=None)
        self.assertEqual((l.council_tax_monthly, l.council_tax_label), (153, "council tax ~C, London average"))
        self.assertIn("bills", l.estimated)
        l = flat(address="Somewhere, London", description="All bills included except council tax")
        self.assertEqual((l.utilities_monthly, l.council_tax_monthly, l.estimated), (0, 153, ["council tax"]))

    def test_default_total_stays_near_the_old_flat_280(self):
        self.assertAlmostEqual(flat(address="Somewhere, London").bills, 280, delta=10)

    def test_rightmove_included_flag_removes_council_tax_only(self):
        l = flat(council_tax_band="C", council_tax_included=True)
        self.assertEqual((l.council_tax_monthly, l.utilities_monthly), (0, config.RENT_BILLS_ESTIMATE))

    def test_bills_included_zeroes_everything(self):
        l = flat(description="Bills included", council_tax_band="H")
        self.assertEqual((l.bills, l.estimated), (0, []))

    def test_band_in_the_advert_text_is_used(self):
        l = flat(description="Council Tax Band: A", address="Somewhere, London")
        self.assertEqual(l.council_tax_monthly, round(2068.47 * 6 / 9 / 12))

    def test_utilities_setting_is_per_person_and_council_tax_stays_separate(self):
        with users.criteria({"bills": 200}):
            l = flat(council_tax_band="B")
        self.assertEqual(l.utilities_monthly, 200)
        self.assertEqual(l.bills, 200 + l.council_tax_monthly)

    def test_rightmove_details_capture_band_and_included_flag(self):
        p = {"livingCosts": {"councilTaxBand": "e", "councilTaxIncluded": False}, "lettings": {}}
        d = rightmove._details(p)
        self.assertEqual((d["council_tax_band"], d["council_tax_included"]), ("E", False))
        d = rightmove._details({"livingCosts": {"councilTaxBand": "ZZ", "councilTaxIncluded": True}})
        self.assertEqual((d["council_tax_band"], d["council_tax_included"]), (None, True))
        d = rightmove._details({})
        self.assertEqual((d["council_tax_band"], d["council_tax_included"]), (None, None))

    def test_refine_applies_the_detail_band_to_the_estimate(self):
        l = flat(rent=1500)
        before = l.council_tax_monthly
        details = {"description": "Bright flat", "council_tax_band": "G", "council_tax_included": None}
        cache._data = {"rm_detail": {"1": details}}
        self.addCleanup(setattr, cache, "_data", {})
        self.assertTrue(rightmove.refine(l))
        self.assertEqual(l.council_tax_band, "G")
        self.assertGreater(l.council_tax_monthly, before)
        self.assertEqual(l.bills, l.council_tax_monthly + l.utilities_monthly)

    def test_stale_detail_cache_without_a_band_key_is_refetched(self):
        l = flat(rent=1500)
        cache._data = {"rm_detail": {"1": {"description": "old entry"}}}
        self.addCleanup(setattr, cache, "_data", {})
        with patch.object(rightmove.net, "page", return_value="<html></html>") as page, \
                patch.object(rightmove, "_property_data", return_value={"id": "1", "livingCosts": {"councilTaxBand": "D"}}):
            self.assertTrue(rightmove.refine(l))
        page.assert_called_once()
        self.assertEqual(l.council_tax_band, "D")


class CapTests(unittest.TestCase):
    def test_rent_between_1520_and_1600_passes_by_default(self):
        for rent in (1520, 1550, 1600):
            l = flat(rent)
            self.assertIsNone(main._cost_reject(l), rent)
            self.assertGreater(l.monthly_cost, config.MAX_RENT)     # all-in is above the rent cap, still fine

    def test_rent_above_the_cap_is_rent_cap_not_cost(self):
        self.assertEqual(main._cost_reject(flat(1601)), "rent_cap")
        with users.criteria({"rent": 1400}):
            self.assertEqual(main._cost_reject(flat(1450)), "rent_cap")
            self.assertIsNone(main._cost_reject(flat(1400)))

    def test_all_in_cap_is_off_by_default_and_applies_when_set(self):
        self.assertIsNone(config.MAX_ALL_IN)
        with users.criteria({"budget": 1800}):
            self.assertEqual(config.MAX_ALL_IN, 1800)
            self.assertIsNone(main._cost_reject(flat(1500, address="Somewhere, London")))     # 1783
            self.assertEqual(main._cost_reject(flat(1550, address="Somewhere, London")), "all_in_cap")  # 1833
        self.assertIsNone(config.MAX_ALL_IN)

    def test_early_gate_only_uses_rent_so_final_bills_cannot_wrongly_reject(self):
        l = flat(1700 - 1000, address="Somewhere, London")   # rent 700: below any cap
        with users.criteria({"budget": 800}):
            self.assertIsNone(main._cost_reject(l, early=True))
            self.assertEqual(main._cost_reject(l), "all_in_cap")
            self.assertEqual(main._cost_reject(flat(900), early=True), "all_in_cap")

    def test_sale_and_scheme_rents_keep_the_old_all_in_cap(self):
        sale = Listing("stb", "9", "u", "a", 1, "Shared Ownership", share_price=100000, share_pct=25,
                       full_value=400000, rent=1100, service_charge=200, min_deposit=10000)
        self.assertEqual(main._cost_reject(sale), "cost")        # £1,600 cap on mortgage + rent + SC
        scheme = Listing("stb", "10", "u", "a", 1, "London Living Rent", tenure="rent", rent=1500,
                         service_charge=200)
        self.assertEqual(main._cost_reject(scheme), "cost")      # bills is None: not costed as a private let
        scheme.service_charge = 50
        self.assertIsNone(main._cost_reject(scheme))

    def test_rental_scope_and_portal_filters_use_the_rent_cap(self):
        l = flat(1590, lat=config.TARGET_LAT, lng=config.TARGET_LNG)
        self.assertTrue(rental_in_scope(l))
        with users.criteria({"rent": 1500}):
            self.assertFalse(rental_in_scope(l))

    def test_ranking_uses_all_in_cost(self):
        cheap, dear = flat(1200), flat(1500)
        for l in (cheap, dear):
            costs.compute(l)
            l.travel_minutes, l.crime_score = 20, 30
        self.assertLess(ranking.score(cheap), ranking.score(dear))
        scale = ranking.cost_scale(cheap)
        self.assertEqual(scale, config.MAX_RENT + config.RENT_BILLS_ESTIMATE + 153)
        with users.criteria({"budget": 2000}):
            self.assertEqual(ranking.cost_scale(cheap), 2000)

    def test_environment_defaults(self):
        self.assertEqual((config.MAX_RENT, config.MAX_ALL_IN, config.MAX_MONTHLY_COST), (1600, None, 1600))


class PriceDropTests(unittest.TestCase):
    def test_rentals_compare_rent_never_the_all_in_figure(self):
        l = flat(1500)
        costs.compute(l)
        prev = {"alerted": True, "cost": 9999, "rent": 1500}      # stale all-in is irrelevant
        self.assertFalse(main.price_dropped(l, prev))
        self.assertTrue(main.price_dropped(l, {"alerted": True, "rent": 1530}))
        self.assertFalse(main.price_dropped(l, {"alerted": True, "rent": 1520}))   # below PRICE_DROP_ALERT

    def test_record_without_a_stored_rent_cannot_alert(self):
        l = flat(1000)
        costs.compute(l)
        self.assertFalse(main.price_dropped(l, {"alerted": True, "cost": 2000}))

    def test_sale_comparison_is_unchanged(self):
        sale = Listing("stb", "9", "u", "a", 1, "SO", monthly_cost=1000)
        self.assertTrue(main.price_dropped(sale, {"cost": 1030}))
        self.assertFalse(main.price_dropped(sale, {"cost": 1020}))

    def test_records_store_rent_only_for_rentals(self):
        l = flat(1400)
        costs.compute(l)
        self.assertEqual(main._record(l, "2026-11-10", True)["rent"], 1400)
        sale = Listing("stb", "9", "u", "a", 1, "SO", monthly_cost=1000, share_price=1)
        self.assertNotIn("rent", main._record(sale, "2026-11-10", True))

    def test_price_cut_tag_uses_rent_for_rentals(self):
        l = flat(1500, address="Somewhere, London")
        costs.compute(l)
        seen = {"first": "2026-11-01T10:00", "cost": 1700, "rent": 1500}     # all-in "fell" by 100+
        self.assertIsNone(intelligence.deal_hunter_signals(l, seen))
        seen["rent"] = 1540
        self.assertEqual(intelligence.deal_hunter_signals(l, seen), "🏷️ Price cut (-£40/mo)")
        self.assertIsNone(intelligence.deal_hunter_signals(l, {"first": "2026-11-01T10:00", "cost": 3000}))
        sale = Listing("stb", "9", "u", "a", 1, "SO", monthly_cost=1000)
        self.assertEqual(intelligence.deal_hunter_signals(sale, {"first": "2026-11-01T10:00", "cost": 1050}),
                         "🏷️ Price cut (-£50/mo)")


class SavedHomeTests(unittest.TestCase):
    def run_pipeline(self, l, info):
        p = {"saved": {l.key: info}, "hidden": [], "hidden_dev": [], "last_report": "2026-11-09T06:30"}
        with patch.object(main, "load_seen", return_value={}), patch.object(prefs, "load", return_value=p), \
                patch.object(prefs, "publish"), patch.object(prefs, "save"), patch.object(main, "save_seen"), \
                patch.object(cache, "save"), patch.object(enrich, "geocode"), \
                patch.object(enrich, "assign_crime_percentiles"), patch.object(main.mapgen, "write"), \
                patch.object(main, "passes_basic", return_value=None), patch.object(main, "passes_area", return_value=None), \
                patch.object(main, "send_report"), patch.object(main, "_ensure_photos"), \
                patch.object(telegram, "send_listing") as send, patch.object(telegram, "throttle"), \
                patch.dict(main.SOURCES, {"rm": type("S", (), {"fetch": staticmethod(lambda: [l])})}), \
                patch("sys.stdout"):
            main.run(["rm"], False, False, report=True, rentals=True)
        return send

    def listing(self, rent, band="C"):
        l = flat(rent, council_tax_band=band, lat=51.52, lng=-0.18)
        costs.compute(l)
        l.travel_minutes, l.crime_score = 25, 30
        return l

    def test_saved_rental_alerts_on_rent_changes_not_bills_changes(self):
        info = {"url": "u", "cost": 1700}                   # old all-in, saved before rents were stored
        send = self.run_pipeline(self.listing(1500), info)
        self.assertEqual((info["rent"], send.call_count), (1500, 0))   # backfilled quietly
        send = self.run_pipeline(self.listing(1500, band="A"), info)  # bills fell, rent did not
        self.assertEqual((info["rent"], send.call_count), (1500, 0))
        self.run_pipeline(self.listing(1450), info)                   # rent cut of £50
        self.assertEqual(info["rent"], 1450)


class CardTests(unittest.TestCase):
    def test_rental_card_leads_with_rent_then_all_in_with_breakdown(self):
        l = flat(1550, council_tax_band="B")
        costs.compute(l)
        line = telegram.finance_lines(l)[0]
        tax = round(2060.3 * 7 / 9 / 12)
        self.assertEqual(line, f"💷 <b>£1,550 rent</b> · ~£{1550 + tax + 130:,} all-in "
                               f"<i>(council tax B £{tax} · utilities ~£130)</i>")

    def test_short_line_shows_rent_and_all_in(self):
        l = flat(1550)
        costs.compute(l)
        l.travel_minutes, l.crime_score = 20, 30
        self.assertIn("£1,550 rent · ~£1,833 all-in", telegram._short_line(l, ""))


if __name__ == "__main__":
    unittest.main()
