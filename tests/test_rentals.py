import datetime as dt
import json
import os
import unittest
from unittest.mock import Mock, patch

from pisobot import cache, costs, main, mapgen, net, telegram
from pisobot.models import Listing
from pisobot.sources import (PRIVATE_RENT, apply_rent_details, cached_search, monthly_rent, otm_rent,
                             private_rent_costs, rent_from_text, rightmove, zoopla)


def next_data(page_props):
    return '<script id="__NEXT_DATA__">' + json.dumps({"props": {"pageProps": page_props}}) + "</script>"


def rm_property(id, **kw):
    return {"id": id, "bedrooms": 1, "displayAddress": "Warwick Avenue W9", "propertySubType": "Flat",
            "summary": "Bright flat", "location": {"latitude": 51.52, "longitude": -0.18},
            "price": {"amount": 1300, "frequency": "monthly"}, "letAvailableDate": "2026-10-19T00:00:00Z",
            "images": [{"url": "property-photo/a/1.jpeg"}],
            "customer": {"mediaServerUrl": "https://media.rightmove.co.uk:443"}, **kw}


class RentalTests(unittest.TestCase):
    def setUp(self):
        cache._data = {}

    def test_rent_frequencies_convert_to_monthly(self):
        self.assertEqual(monthly_rent(300, "weekly"), 1300)
        self.assertEqual(monthly_rent(18000, "yearly"), 1500)
        self.assertEqual(rent_from_text("£1,540 pcm (£355 pw)"), 1540)
        self.assertEqual(rent_from_text("£300 pw"), 1300)

    def test_bills_are_estimated_unless_included_and_deposit_is_five_weeks(self):
        l = private_rent_costs(Listing("rm", "1", "u", "a", 1, PRIVATE_RENT, tenure="rent", rent=1300))
        self.assertEqual((l.bills, l.estimated, l.min_deposit), (280, ["bills"], 1500))
        self.assertTrue(costs.compute(l))
        self.assertEqual((l.monthly_cost, l.deposit), (1580, 1500))
        inc = private_rent_costs(Listing("rm", "2", "u", "a", 1, PRIVATE_RENT, tenure="rent", rent=1500,
                                         description="Lovely flat, all bills included"))
        self.assertEqual((inc.bills, inc.estimated), (0, []))
        exc = private_rent_costs(Listing("rm", "3", "u", "a", 1, PRIVATE_RENT, tenure="rent", rent=1500,
                                         description="Bills included? No - bills not included"))
        self.assertEqual(exc.bills, 280)
        ct = private_rent_costs(Listing("rm", "4", "u", "a", 1, PRIVATE_RENT, tenure="rent", rent=1400,
                                        description="*BILLS INCLUDED (except Council Tax)*"))
        self.assertEqual((ct.bills, ct.estimated), (150, ["council tax"]))

    def test_rightmove_skips_shares_and_students_and_reads_full_size_photos(self):
        props = [rm_property(1), rm_property(2, propertySubType="House Share"), rm_property(3, students=True),
                 rm_property(4, price={"amount": 300, "frequency": "weekly"})]
        html = next_data({"searchResults": {"properties": props, "resultCount": "4"}})
        with patch.object(net, "page", return_value=html) as page:
            homes = rightmove.search()
        self.assertEqual([h.id for h in homes], ["1", "4"])
        self.assertEqual(page.call_count, 1)
        self.assertEqual(page.call_args.kwargs["params"]["locationIdentifier"], "STATION^6953")
        self.assertEqual(homes[0].images, ["https://media.rightmove.co.uk:443/property-photo/a/1.jpeg"])
        self.assertEqual((homes[0].available, homes[0].url), ("2026-10-19", "https://www.rightmove.co.uk/properties/1"))
        self.assertEqual(homes[1].rent, 1300)

    def test_onthemarket_rent_skips_rooms(self):
        rows = [{"id": "1", "property-title": "1 bedroom flat to rent", "price": "£1,400 pcm (£323 pw)",
                 "bedrooms": 1, "address": "Home SE22", "location": {"lat": 51.4, "lon": -0.07}},
                {"id": "2", "property-title": "Room to rent", "price": "£900 pcm", "bedrooms": 1}]
        html = next_data({"initialReduxState": {"results": {"list": rows, "paginationControls": {}}}})
        with patch.object(net, "page", return_value=html):
            homes = otm_rent.search()
        self.assertEqual([(h.source, h.id, h.rent, h.tenure) for h in homes], [("otmr", "1", 1400, "rent")])

    def test_zoopla_reads_the_server_components_stream(self):
        rows = [{"listingId": "7", "price": "£1,300 pcm", "title": "1 bed flat to rent", "address": "Home W6",
                 "features": [{"iconId": "bed", "content": 1}], "pos": {"lat": 51.49, "lng": -0.22},
                 "gallery": ["a.jpg"], "sizeSqft": 480, "availableFrom": "1st Dec 2026",
                 "listingUris": {"detail": "/to-rent/details/7/"}, "summaryDescription": "Bright flat"},
                {"listingId": "8", "price": "£750 pcm", "title": "1 bed flat to rent", "summaryDescription": "Double room"}]
        payload = json.dumps({"regularListingsFormatted": rows, "pagination": {"pageNumber": 1, "pageNumberMax": 1}})
        html = f'<script>self.__next_f.push([1,{json.dumps("5:" + payload)}])</script>'
        with patch.object(net, "page", return_value=html) as page:
            homes = zoopla.search()
        self.assertEqual(page.call_args.args[0], "https://www.zoopla.co.uk/to-rent/flats/oxford-circus/")
        self.assertEqual(page.call_args.kwargs["params"]["is_shared_accommodation"], "false")
        self.assertEqual(len(homes), 1)
        h = homes[0]
        self.assertEqual((h.key, h.bedrooms, h.rent, h.available, h.floor_area_m2, h.url),
                         ("zp:7", 1, 1300, "2026-12-01", 44.59, "https://www.zoopla.co.uk/to-rent/details/7/"))
        self.assertEqual(h.images, ["https://lid.zoocdn.com/u/1024/768/a.jpg"])
        detail = json.dumps({"listingId": 7, "features": {"bullets": ["Communal bathroom"]},
                             "floorPlan": {"image": [{"filename": "p.jpg"}]},
                             "detailedDescription": "All bills included<br>First floor flat"}, separators=(",", ":"))
        with patch.object(net, "page", return_value=f'<script>self.__next_f.push([1,{json.dumps("6:" + detail)}])</script>'):
            self.assertTrue(zoopla.refine(h))
        self.assertEqual((h.bills, h.floorplan), (0, "https://lid.zoocdn.com/u/1024/768/p.jpg"))
        self.assertTrue(h.excluded_by_keywords())

    def test_blocked_page_fails_the_source_instead_of_returning_nothing(self):
        with patch.object(net, "page", return_value="<html>Just a moment...</html>"):
            with self.assertRaises(ValueError):
                rightmove.search()

    def test_search_is_reused_from_cache_until_refresh_is_due(self):
        home = private_rent_costs(Listing("rm", "1", "u", "a", 1, PRIVATE_RENT, tenure="rent", rent=1300))
        search = Mock(return_value=[home])
        first = cached_search("rm", search)
        again = cached_search("rm", search)
        self.assertEqual(search.call_count, 1)
        self.assertEqual((again[0].key, again[0].rent, again[0].bills), (first[0].key, 1300, 280))
        stale = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=12)).isoformat()
        cache._data["rent_search"]["rm"]["at"] = stale
        cached_search("rm", search)
        self.assertEqual(search.call_count, 2)

    def test_decodo_is_used_only_with_a_token(self):
        with patch.dict(os.environ, {"DECODO_AUTH_TOKEN": ""}), \
                patch.object(net, "get", return_value=Mock(text="direct")):
            self.assertEqual(net.page("https://example.com"), "direct")
        reply = Mock(status_code=200, json=lambda: {"results": [{"content": "<html>ok</html>", "status_code": 200}]})
        with patch.dict(os.environ, {"DECODO_AUTH_TOKEN": "tok"}), \
                patch.object(net.session, "post", return_value=reply) as post:
            self.assertEqual(net.page("https://example.com/a", params={"b": 1}), "<html>ok</html>")
        self.assertEqual(post.call_args.kwargs["json"],
                         {"target": "universal", "url": "https://example.com/a?b=1", "geo": "United Kingdom"})
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer tok")

    def test_rightmove_details_reject_rooms_and_use_the_advertised_deposit(self):
        flat = [{"propertyData": 1}, {"id": 2, "lettings": 3, "sizings": 7, "text": 10, "keyFeatures": 12},
                "9", {"letAvailableDate": 4, "deposit": 5, "letType": 6}, "19/10/2026", 1600, "Long term",
                [8], {"unit": 9, "minimumSize": 13}, "sqm", {"description": 11},
                "Bright one bedroom flat. Bills not included", [], 40]
        html = "<script>window.__PAGE_MODEL = " + json.dumps({"data": json.dumps(flat)}) + ";</script>"
        home = private_rent_costs(Listing("rm", "9", "https://rm/9", "a", 1, PRIVATE_RENT, tenure="rent", rent=1300))
        with patch.object(net, "page", return_value=html):
            self.assertTrue(rightmove.refine(home))
        self.assertEqual((home.floor_area_m2, home.available, home.min_deposit), (40, "2026-10-19", 1600))
        flat[-1] = 18
        cache._data = {}
        small = private_rent_costs(Listing("rm", "9", "https://rm/9", "a", 1, PRIVATE_RENT, tenure="rent", rent=1300))
        with patch.object(net, "page", return_value="<script>window.__PAGE_MODEL = "
                          + json.dumps({"data": json.dumps(flat)}) + ";</script>"):
            self.assertFalse(rightmove.refine(small))

    def test_short_lets_and_shared_bathrooms_are_excluded_after_refine(self):
        for words in ("Short term let", "Double room, shared bathroom"):
            home = private_rent_costs(Listing("otmr", "1", "u", "a", 1, PRIVATE_RENT, tenure="rent",
                                              rent=1200, description=words, lat=51.51, lng=-0.14))
            self.assertTrue(home.excluded_by_keywords())

    def test_rooms_filed_as_flats_are_dropped_by_their_description(self):
        rooms = ["A comfortable ensuite room is available in shared accommodation",
                 "Large double bedroom in a substantial house. Sharing a spacious kitchen and bathroom",
                 "Beautiful all-inclusive co-living studios"]
        flats = ["Self-contained one bedroom flat with en-suite bathroom and a double bedroom available now",
                 "Share of freehold, private balcony"]
        for words, keep in [(w, False) for w in rooms] + [(w, True) for w in flats]:
            home = Listing("otmr", "1", "u", "a", 1, PRIVATE_RENT, tenure="rent", rent=1300)
            self.assertEqual(apply_rent_details(home, {"description": words}), keep, words)

    def test_onthemarket_rental_details_read_the_published_deposit(self):
        p = {"id": "5", "lettingDetails": {"items": ["Furnished", "Deposit: £1172.00", "Long term let"]},
             "description": "One bedroom flat", "minimumAreaSqFt": "450"}
        html = next_data({"initialReduxState": {"property": p}})
        home = private_rent_costs(Listing("otmr", "5", "https://otm/5", "a", 1, PRIVATE_RENT, tenure="rent", rent=1015))
        with patch.object(net, "page", return_value=html):
            self.assertTrue(otm_rent.refine(home))
        self.assertEqual((home.min_deposit, home.floor_area_m2), (1172, 41.81))

    def test_advert_service_charge_is_not_added_to_rent(self):
        home = private_rent_costs(Listing("rm", "1", "u", "a", 1, PRIVATE_RENT, tenure="rent", rent=1200,
                                          description="Service charge £200 per month paid by landlord",
                                          lat=51.51, lng=-0.14))
        with patch.object(rightmove, "refine", return_value=True):
            self.assertIsNone(main.passes_basic(home))
        self.assertEqual(home.monthly_cost, 1480)

    def test_report_lists_rentals_even_when_cheaper_homes_fill_the_cheapest_places(self):
        def home(i, **kw):
            values = dict(source="stb", id=str(i), url=f"https://example.com/{i}", address=f"Home {i}", bedrooms=1,
                          scheme="Shared Ownership", monthly_cost=900 + i, deposit=10000, share_price=100000,
                          travel_minutes=20, crime_score=30, lat=51.5 + i / 100, lng=-0.1)
            values.update(kw)
            return Listing(**values)
        cheap = [home(i) for i in range(8)]
        rented = home(50, source="rm", scheme=PRIVATE_RENT, tenure="rent", monthly_cost=1500,
                      rent=1220, bills=280, url="https://www.rightmove.co.uk/properties/50", address="Rental flat")
        text = "\n".join(t for t, _ in telegram.report_messages([], cheap + [rented], [], "Report", lambda l: ""))
        self.assertIn("Private rentals · 1", text)
        self.assertIn('href="https://www.rightmove.co.uk/properties/50"', text)
        self.assertIn("Other 8 homes", text)

    def test_map_cards_link_straight_to_the_original_listing(self):
        self.assertIn('<a class="open" href="${esc(d.url)}" target="_blank"', mapgen.TEMPLATE)
        self.assertIn("ev.target.closest('a')", mapgen.TEMPLATE)

    def test_rental_card_shows_bills_deposit_and_move_in(self):
        l = private_rent_costs(Listing("rm", "1", "u", "a", 1, PRIVATE_RENT, tenure="rent", rent=1300,
                                       available="2026-10-19"))
        costs.compute(l)
        lines = telegram.finance_lines(l)
        self.assertIn("Bills: ~£280", lines[0])
        self.assertIn("£1.5k dep", lines[1])
        self.assertIn("from 2026-10-19", lines[1])


if __name__ == "__main__":
    unittest.main()
