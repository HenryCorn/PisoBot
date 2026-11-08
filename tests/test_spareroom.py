from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from requests import HTTPError

from pisobot import cache, config, main, telegram, watch
from pisobot.sources import spareroom

FIXTURE = (Path(__file__).parent / "fixtures_spareroom.html").read_text(encoding="utf-8")
NEAR = (51.52, -0.13)


def page(cards, more=False):
    return cards + ('<a href="/flats-to-rent/london/page2">2</a>' if more else "")


class SpareRoomTests(unittest.TestCase):
    def setUp(self):
        patch.object(cache, "_data", {}).start()
        self.addCleanup(patch.stopall)
        self.coords_patch = patch.object(spareroom, "_coords", return_value=NEAR)
        self.coords = self.coords_patch.start()
        patch.object(config, "MAX_MONTHLY_COST", 3000).start()

    def test_real_cards_parse_rent_beds_bills_and_links(self):
        with patch.object(spareroom.net, "page", return_value=page(FIXTURE)):
            listings = {l.id: l for l in spareroom.search()}
        self.assertEqual(set(listings), {"15507998", "18489813", "18393773"})
        weekly = listings["15507998"]
        self.assertEqual((weekly.bedrooms, weekly.rent), (2, round(468 * 52 / 12, 2)))
        self.assertTrue(weekly.url.startswith("https://www.spareroom.co.uk/flats-to-rent/london/"))
        self.assertNotIn("?", weekly.url)
        self.assertTrue(weekly.image.startswith("https://photos"))
        monthly = listings["18489813"]
        self.assertEqual((monthly.bedrooms, monthly.rent, monthly.available), (3, 2295, "2026-11-22"))
        bills_included = listings["18393773"]
        self.assertEqual((bills_included.rent, bills_included.bills), (1275, 0))
        self.assertEqual(bills_included.tenure, "rent")

    def test_full_postcode_in_text_beats_district_centre(self):
        with patch.object(spareroom.net, "page", return_value=page(FIXTURE)):
            listings = {l.id: l for l in spareroom.search()}
        exact = listings["15507998"]   # its text quotes E14 3LH
        self.assertIn("E14 3LH", exact.address)
        self.assertNotIn("approximate", exact.description)
        self.assertIn(("postcode", "E14 3LH"), [c.args for c in self.coords.call_args_list])
        self.assertIn("approximate", listings["18489813"].description)
        self.assertIn("commute/crime estimated", telegram.facts_line(listings["18489813"]))
        self.assertNotIn("commute/crime estimated", telegram.facts_line(exact))

    def test_filters_budget_beds_distance_rooms_and_other_types(self):
        card = ('<li class="listing-result" data-listing-id="{id}" data-listing-type="{type}" '
                'data-listing-title="{title}" data-listing-postcode="W1" data-listing-property-type="flat" '
                'data-listing-rooms-in-property="{beds}" data-listing-ad-headline-rate="&pound;{rent}" '
                'data-listing-ad-headline-rate-period="pcm"></li>')
        cards = "".join(card.format(**c) for c in (
            dict(id=1, type="flatstorent", title="Nice flat", beds=1, rent="1,300"),
            dict(id=2, type="offered", title="Nice flat", beds=1, rent="900"),
            dict(id=3, type="flatstorent", title="Double room in flat", beds=1, rent="1,100"),
            dict(id=4, type="flatstorent", title="Nice flat", beds=1, rent="3,500"),
            dict(id=5, type="flatstorent", title="Nice flat", beds=0, rent="1,200"),
            dict(id=6, type="flatstorent", title="Flat with ensuite and living room", beds=2, rent="1,300"),
        ))
        with patch.object(spareroom.net, "page", return_value=cards):
            self.assertEqual([l.id for l in spareroom.search()], ["1", "6"])
        self.coords.return_value = (51.40, 0.10)   # ~20 km away
        with patch.object(spareroom.net, "page", return_value=cards):
            self.assertEqual(spareroom.search(), [])

    def test_paging_is_bounded_and_empty_first_page_is_a_failure(self):
        with patch.object(config, "RENT_MAX_PAGES", 2), \
                patch.object(spareroom.net, "page", return_value=page(FIXTURE, more=True)) as get:
            spareroom.search()
        self.assertEqual([c.args[0] for c in get.call_args_list],
                         [spareroom.LIST, f"{spareroom.LIST}/page2"])
        with patch.object(spareroom.net, "page", return_value="<html>blocked</html>"):
            with self.assertRaisesRegex(ValueError, "no listing cards"):
                spareroom.search()
        with patch.object(spareroom.net, "page", side_effect=[page(FIXTURE, more=True), "<html>blocked</html>"]):
            with self.assertRaisesRegex(ValueError, "no listing cards"):
                spareroom.search()

    def test_postcode_lookups_are_cached(self):
        self.coords_patch.stop()
        response = Mock(status_code=200, json=Mock(return_value={"result": {"latitude": 51.5, "longitude": -0.1}}))
        with patch.object(spareroom.net.session, "get", return_value=response) as get:
            self.assertEqual(spareroom._coords("outcode", "E14"), (51.5, -0.1))
            self.assertEqual(spareroom._coords("outcode", "E14"), (51.5, -0.1))
        self.assertEqual(get.call_count, 1)

    def test_postcode_outages_and_invalid_coordinates_do_not_become_empty_successes(self):
        self.coords_patch.stop()
        for response, error in (
                (Mock(status_code=503, raise_for_status=Mock(side_effect=HTTPError("unavailable"))), HTTPError),
                (Mock(status_code=200, json=Mock(return_value={"result": None})), ValueError),
                (Mock(status_code=200, json=Mock(return_value={
                    "result": {"latitude": 51.5, "longitude": None}})), ValueError)):
            with self.subTest(response=response), patch.object(spareroom.net.session, "get", return_value=response):
                with self.assertRaises(error):
                    spareroom._coords("outcode", "E14")
                self.assertIsNone(cache.get("outcode", "E14"))

    def test_changed_filters_do_not_reuse_an_old_search(self):
        with patch.object(spareroom, "search", return_value=[]) as search:
            spareroom.fetch()
            spareroom.fetch()
            self.assertEqual(search.call_count, 1)
            with patch.object(config, "RENT_MIN", config.RENT_MIN + 100):
                spareroom.fetch()
            self.assertEqual(search.call_count, 2)

    def test_registered_for_the_vps_watch(self):
        self.assertIn("sr", main.LOCAL_RENTAL_SOURCES)
        self.assertEqual(watch.Watcher(["sr"]).sources, ["sr"])


if __name__ == "__main__":
    unittest.main()
