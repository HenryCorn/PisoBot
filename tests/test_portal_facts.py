"""Fast-poll correctness and the free facts the portals already publish."""
import datetime as dt
import json
import unittest
from unittest.mock import patch

from pisobot import cache, config, net, telegram
from pisobot.models import Listing
from pisobot.sources import PRIVATE_RENT, cached_search, otm_rent, private_rent_costs, rightmove

TODAY = dt.date(2026, 11, 10)


def next_data(page_props):
    return '<script id="__NEXT_DATA__">' + json.dumps({"props": {"pageProps": page_props}}) + "</script>"


def rm_property(id, **kw):
    return {"id": id, "bedrooms": 1, "displayAddress": "Warwick Avenue W9", "propertySubType": "Flat",
            "summary": "Bright flat", "location": {"latitude": 51.52, "longitude": -0.18},
            "price": {"amount": 1300, "frequency": "monthly"}, **kw}


def otm_card(id="1", **kw):
    return {"id": id, "property-title": "1 bedroom flat to rent", "humanised-property-type": "Flat",
            "price": "£1,400 pcm", "bedrooms": 1, "address": "Home SE22",
            "location": {"lat": 51.4, "lon": -0.07}, **kw}


class OnTheMarketHeadPollTests(unittest.TestCase):
    def test_every_page_asks_for_newest_first(self):
        def page(url, params=None):
            number = params.get("page", 1)
            rows = [otm_card(str(number))]
            return next_data({"initialReduxState": {"results": {
                "list": rows, "paginationControls": {"next": "x"} if number < 3 else {}}}})
        with patch.object(net, "page", side_effect=page) as get:
            homes = otm_rent.search()
        self.assertEqual([h.id for h in homes], ["1", "2", "3"])
        self.assertEqual([c.kwargs["params"]["sort-field"] for c in get.call_args_list], ["update_date"] * 3)
        self.assertEqual(get.call_args_list[0].kwargs["params"]["max-price"], int(config.MAX_RENT))


class CapturedFactsTests(unittest.TestCase):
    def test_rightmove_search_card_facts(self):
        home = rightmove._listing(rm_property(
            5, firstVisibleDate="2026-11-08T09:30:00Z", bathrooms=2, formattedBranchName=" by Foxtons, Chiswick",
            listingUpdate={"listingUpdateReason": "price_reduced", "listingUpdateDate": "2026-11-09T07:00:00Z"}))
        self.assertEqual((home.first_listed, home.update_reason, home.update_date, home.bathrooms, home.agent),
                         ("2026-11-08", "reduced", "2026-11-09", 2, "Foxtons, Chiswick"))
        plain = rightmove._listing(rm_property(6, listingUpdate={"listingUpdateReason": "weird"}))
        self.assertEqual((plain.first_listed, plain.update_reason, plain.update_date, plain.bathrooms, plain.agent),
                         (None, None, None, None, None))
        new = rightmove._listing(rm_property(7, listingUpdate={"listingUpdateReason": "new",
                                                               "listingUpdateDate": "2026-11-10T01:00:00Z"}))
        self.assertEqual((new.update_reason, new.update_date), ("added", "2026-11-10"))

    def test_onthemarket_search_card_facts(self):
        with patch.object(otm_rent, "_today", return_value=TODAY):
            home = otm_rent._listing(otm_card(**{
                "days-since-added-reduced": 3, "agent": {"name": "Dexters  Hackney"}, "bathrooms": 1,
                "fees-description": "Holding deposit: £323. Security deposit: £1,615 (5 weeks)"}))
        self.assertEqual((home.update_reason, home.update_date, home.agent, home.bathrooms),
                         ("added_or_reduced", "2026-11-07", "Dexters Hackney", 1))
        self.assertEqual(home.advert_deposit, 323)       # the first "deposit" amount on the card
        self.assertEqual(home.min_deposit, 323)
        string_agent = otm_rent._listing(otm_card(agent="Foxtons", **{"days-since-added-reduced": "0"}))
        self.assertEqual(string_agent.agent, "Foxtons")
        self.assertEqual(string_agent.update_date, dt.date.today().isoformat())   # "0" days = today
        self.assertIsNone(string_agent.advert_deposit)
        self.assertEqual(string_agent.min_deposit, round(1400 * 12 / 52 * config.RENT_DEPOSIT_WEEKS))

    def test_advert_deposit_survives_a_per_person_bills_recalculation(self):
        home = otm_rent._listing(otm_card(**{"fees-description": "Deposit: £1,500"}))
        self.assertEqual(private_rent_costs(home).min_deposit, 1500)

    def test_facts_survive_the_search_cache(self):
        cache._data = {}
        self.addCleanup(setattr, cache, "_data", {})
        home = rightmove._listing(rm_property(8, firstVisibleDate="2026-11-08", bathrooms=1,
                                              formattedBranchName="by Dexters, Hackney"))
        cached_search("rm", lambda: [home])
        [again] = cached_search("rm", lambda: self.fail("should be cached"))
        self.assertEqual((again.first_listed, again.bathrooms, again.agent),
                         ("2026-11-08", 1, "Dexters, Hackney"))


class CardTests(unittest.TestCase):
    def listing(self, **kw):
        values = dict(source="rm", id="1", url="u", address="Warwick Avenue, London W9", bedrooms=1,
                      scheme=PRIVATE_RENT, tenure="rent", rent=1300)
        values.update(kw)
        return private_rent_costs(Listing(**values))

    def test_age_wording(self):
        cases = [
            (dict(update_reason="added", update_date="2026-11-10"), "Added today"),
            (dict(update_reason="reduced", update_date="2026-11-09"), "Reduced yesterday"),
            (dict(update_reason="added_or_reduced", update_date="2026-11-07"), "Added/reduced 3 days ago"),
            (dict(first_listed="2026-11-05"), "Added 5 days ago"),
            (dict(update_reason="reduced", update_date=None, first_listed="2026-11-10"), "Added today"),
            (dict(first_listed="2026-11-20"), ""),     # a future date is not shown
            ({}, ""),
        ]
        for fields, expected in cases:
            with self.subTest(fields=fields):
                self.assertEqual(telegram.age_text(self.listing(**fields), TODAY), expected)

    def test_card_shows_age_and_agent(self):
        l = self.listing(update_reason="reduced", update_date="2026-11-10", agent="Foxtons <Chiswick>")
        l.travel_minutes, l.crime_score = 20, 30
        self.assertEqual(telegram.portal_line(l, TODAY), "🕒 Reduced today · 🏢 Foxtons &lt;Chiswick&gt;")
        with patch.object(telegram, "_today", return_value=TODAY):
            text = telegram.format_listing(l)
        self.assertIn("🕒 Reduced today · 🏢 Foxtons &lt;Chiswick&gt;", text)

    def test_card_without_portal_facts_is_unchanged(self):
        l = self.listing()
        l.travel_minutes, l.crime_score = 20, 30
        self.assertEqual(telegram.portal_line(l, TODAY), "")
        self.assertNotIn("🕒", telegram.format_listing(l))
        self.assertNotIn("🏢", telegram.format_listing(l))


class RightmoveEndOfResultsTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(config, "RENT_MAX_PAGES", 60)
        patcher.start()
        self.addCleanup(patcher.stop)

    def pages(self, last_index_with_data):
        def page(url, params=None):
            index = params["index"]
            if index > last_index_with_data:
                return "<html><body>We couldn't find the place you're looking for</body></html>"
            return next_data({"searchResults": {"resultCount": "1,268", "properties": [rm_property(index + 1)]}})
        return page

    def test_a_200_page_without_data_at_the_pagination_cap_ends_cleanly(self):
        with patch.object(net, "page", side_effect=self.pages(1007)) as get:
            homes = rightmove.search()
        self.assertEqual(len(homes), 42)                       # indexes 0, 24 ... 984
        self.assertEqual(get.call_args_list[-1].kwargs["params"]["index"], 1008)
        self.assertEqual(get.call_count, 43)

    def test_a_missing_page_before_the_cap_is_still_a_block(self):
        with patch.object(net, "page", side_effect=self.pages(23)):
            with self.assertRaisesRegex(ValueError, "blocked or layout changed"):
                rightmove.search()

    def test_first_page_without_data_is_still_a_block(self):
        with patch.object(net, "page", return_value="<html>Just a moment...</html>"):
            with self.assertRaises(ValueError):
                rightmove.search()


if __name__ == "__main__":
    unittest.main()
