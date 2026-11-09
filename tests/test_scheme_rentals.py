import contextlib
import datetime as dt
import json
import unittest
from unittest.mock import patch

from pisobot import cache, config, main, telegram, users, watch
from pisobot.models import Listing
from pisobot.sources import lq, sharetobuy


def llr(i=1, rent=1100.0):
    return Listing("stb", str(i), f"https://www.sharetobuy.com/properties/{i}/", "Road, London, W1D 4AA", 1,
                   "London Living Rent", tenure="rent", rent=rent, service_charge=0, min_deposit=110.0,
                   lat=51.52, lng=-0.13)


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
            entry = next(iter(cache._data["rent_search"].values()))
            entry["at"] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).isoformat()
            main.SOURCES["stbr"].fetch()
            self.assertEqual(fetch.call_count, 1)
            entry["at"] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=4)).isoformat()
            main.SOURCES["stbr"].fetch()
            self.assertEqual(fetch.call_count, 2)
            with patch.object(config, "MAX_MONTHLY_COST", 2500):
                main.SOURCES["stbr"].fetch()
            self.assertEqual(fetch.call_count, 3)

    def test_lq_rent_only_skips_sale_plots(self):
        dev = {"development_location": {"lat": "51.5", "lng": "-0.14", "address": "W1"}, "site_live": True,
               "plots_available": True, "name": "Dev", "url": "https://lqhomes.com/dev/", "id": 7}
        plots = [{"ID": 1, "post_title": "Plot 1", "post_name": "plot-1",
                  "acf": {"plot_type": "london_living_rent", "plot_availability": "available",
                          "plot_bedrooms": "1", "plot_monthly_rent": "£1,050"}},
                 {"ID": 2, "post_title": "Plot 2", "post_name": "plot-2",
                  "acf": {"plot_type": "shared_ownership", "plot_availability": "available",
                          "plot_bedrooms": "1", "plot_share_price": "£90,000"}}]
        with patch.object(lq.net, "page", side_effect=[json.dumps([dev]), json.dumps(plots)]) as page, \
                patch.object(lq.net, "get") as legacy:
            [l] = lq.search_rent()
        self.assertEqual((l.key, l.scheme, l.rent, l.bills), ("lq:1", "London Living Rent", 1050, config.RENT_BILLS_ESTIMATE))
        self.assertEqual(page.call_count, 2)
        legacy.assert_not_called()

    def test_scheme_rentals_apply_radius_and_minimum_rent_and_cache_them_separately(self):
        for module in (sharetobuy, lq):
            home = llr()
            home.lat = config.TARGET_LAT + 0.06
            home.lng = config.TARGET_LNG
            with self.subTest(module=module.__name__), patch.object(module, "fetch", return_value=[home]) as fetch, \
                    patch.object(config, "RENT_SEARCH_MILES", 5), patch.object(config, "RENT_MIN", 1000):
                self.assertEqual(len(module.fetch_rent()), 1)
                with patch.object(config, "RENT_SEARCH_MILES", 1):
                    self.assertEqual(module.fetch_rent(), [])
                with patch.object(config, "RENT_MIN", 1200):
                    self.assertEqual(module.fetch_rent(), [])
                self.assertEqual(len(module.fetch_rent()), 1)
                self.assertEqual(fetch.call_count, 3)

    def test_sharetobuy_empty_results_are_distinct_from_layout_failures_and_paced(self):
        empty = '<div class="search-results-list-wrapper"><h2>No results found</h2></div>'
        with patch.object(sharetobuy.net, "page", return_value=empty) as page, \
                patch.object(sharetobuy.net, "get") as legacy, patch.object(config, "RENT_SEARCH_MILES", 3):
            self.assertEqual(sharetobuy.fetch(rent_only=True), [])
        self.assertEqual(dict(page.call_args.kwargs["params"])["radius"], 3)
        legacy.assert_not_called()
        with patch.object(sharetobuy.net, "page", return_value="<html>Unexpected page</html>"):
            with self.assertRaisesRegex(ValueError, "explicit empty result"):
                sharetobuy.fetch(rent_only=True)

    def test_watch_accepts_scheme_sources_and_keeps_their_cache_between_cycles(self):
        owner = users.User("1", owner=True)
        with patch.object(users, "everyone", return_value=[owner]), \
                patch.object(main, "locked", return_value=contextlib.nullcontext()), \
                patch.object(main, "_run", return_value={"matches": 0, "complete": True}), \
                patch.object(watch, "_meta", return_value=None), \
                patch.object(sharetobuy, "fetch", return_value=[llr()]) as fetch:
            watcher = watch.Watcher(["stbr"])
            watcher.cycle(dry_run=True)
            watcher.cycle(dry_run=True)
            self.assertEqual(fetch.call_count, 1)
        with self.assertRaises(ValueError):
            watch.Watcher(["zp"])

    def test_cards_name_the_scheme_and_flag_eligibility(self):
        l = llr()
        sharetobuy.private_rent_costs(l)
        l.monthly_cost, l.deposit = 1380, l.min_deposit
        self.assertIn("London Living Rent, income caps apply", telegram.headline(l))
        private = Listing("rm", "1", "u", "a", 1, "Private rent", tenure="rent", rent=1200, monthly_cost=1480)
        self.assertNotIn("income caps", telegram.headline(private))


if __name__ == "__main__":
    unittest.main()
