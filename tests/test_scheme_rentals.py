import unittest
from unittest.mock import patch

from pisobot import cache, config, main, telegram, watch
from pisobot.models import Listing
from pisobot.sources import lq, sharetobuy


def llr(i=1, rent=1100.0):
    return Listing("stb", str(i), f"https://www.sharetobuy.com/properties/{i}/", "Road, London, SE10 0JT", 1,
                   "London Living Rent", tenure="rent", rent=rent, service_charge=0, min_deposit=110.0,
                   lat=51.49, lng=0.0)


class SchemeRentalTests(unittest.TestCase):
    def setUp(self):
        patch.object(cache, "_data", {}).start()
        self.addCleanup(patch.stopall)

    def test_rent_only_searches_follow_the_current_budget(self):
        with patch.object(config, "MAX_MONTHLY_COST", 1900):
            self.assertEqual(sharetobuy._searches(rent_only=True), [("2", ["13", "17"], 1900)])
            self.assertEqual(len(sharetobuy._searches()), 2)

    def test_scheme_rents_are_costed_like_private_lets(self):
        with patch.object(sharetobuy, "fetch", return_value=[llr()]):
            [l] = sharetobuy.search_rent()
        self.assertEqual((l.bills, l.service_charge), (config.RENT_BILLS_ESTIMATE, 0))
        self.assertEqual(l.min_deposit, round(1100 * 12 / 52 * 5))
        self.assertIn("bills", l.estimated)

    def test_slow_sources_are_cached_per_filter_set(self):
        with patch.object(sharetobuy, "fetch", return_value=[llr()]) as fetch:
            main.SOURCES["stbr"].fetch()
            main.SOURCES["stbr"].fetch()
            self.assertEqual(fetch.call_count, 1)
            with patch.object(config, "MAX_MONTHLY_COST", 2500):
                main.SOURCES["stbr"].fetch()
            self.assertEqual(fetch.call_count, 2)

    def test_lq_rent_only_skips_sale_plots(self):
        dev = {"development_location": {"lat": "51.5", "lng": "-0.14", "address": "W1"}, "site_live": True,
               "plots_available": True, "name": "Dev", "url": "https://lqhomes.com/dev/", "id": 7}
        plots = [{"ID": 1, "post_title": "Plot 1", "post_name": "plot-1",
                  "acf": {"plot_type": "london_living_rent", "plot_availability": "available",
                          "plot_bedrooms": "1", "plot_monthly_rent": "£1,050"}},
                 {"ID": 2, "post_title": "Plot 2", "post_name": "plot-2",
                  "acf": {"plot_type": "shared_ownership", "plot_availability": "available",
                          "plot_bedrooms": "1", "plot_share_price": "£90,000"}}]
        responses = iter([[dev], plots])
        with patch.object(lq.net, "get", side_effect=lambda *a, **k: type("R", (), {"json": lambda s: next(responses)})()):
            [l] = lq.search_rent()
        self.assertEqual((l.key, l.scheme, l.rent, l.bills), ("lq:1", "London Living Rent", 1050, config.RENT_BILLS_ESTIMATE))

    def test_watch_accepts_scheme_sources_and_keeps_their_cache_between_cycles(self):
        self.assertEqual(watch.Watcher(["rm", "stbr", "lqr"]).sources, ["rm", "stbr", "lqr"])
        with self.assertRaises(ValueError):
            watch.Watcher(["zp"])

    def test_cards_name_the_scheme_and_flag_eligibility(self):
        l = llr()
        sharetobuy.private_rent_costs(l)
        l.monthly_cost, l.deposit = 1380, l.min_deposit
        self.assertIn("London Living Rent", telegram.finance_lines(l)[1])
        self.assertIn("income caps apply", telegram.facts_line(l))
        private = Listing("rm", "1", "u", "a", 1, "Private rent", tenure="rent", rent=1200)
        self.assertNotIn("income caps", telegram.facts_line(private))


if __name__ == "__main__":
    unittest.main()
