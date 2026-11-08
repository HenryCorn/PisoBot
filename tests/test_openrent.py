import datetime as dt
import io
import json
import unittest
from unittest.mock import Mock, patch

from pisobot import cache, config, net
from pisobot.sources import cached_search, openrent


def index_html(rows=None):
    """Sanitized structure of the site's actual inline JS, including trailing commas."""
    rows = [index_row()] if rows is None else rows
    arrays = "\n".join(f"var {key} = [{','.join(str(row[key]) for row in rows)}{',' if rows else ''}];"
                       for key in openrent.ARRAYS)
    return f"""<html><script>
        var SEARCHLAT = 51.5152; var SEARCHLNG = -0.1419;
        var SEARCHRADIUS = -1; SEARCHRADIUS = 9;
        var NUMBEROFPROPERTIES = {len(rows)};
        {arrays}
        var lockedForHours = 0; var userVtState = parseInt(0);
        if (userVtState === 2) {{ lockedForHours = 12; }}
        else if (userVtState === 0) {{ lockedForHours = 24; }}
        </script></html>"""


def index_row(identifier=700001, **kwargs):
    return {"PROPERTYIDS": identifier, "PROPERTYLISTLATITUDES": 51.52,
            "PROPERTYLISTLONGITUDES": -0.15, "prices": 1300, "bedrooms": 1,
            "isshared": 0, "isstudio": 0, "nonStudents": 1, "islivelistBool": 1,
            "dateFirstListedMs": 1700000000000 + identifier, "propertyTypes": 2, "bills": 0, **kwargs}


def card(identifier=700001, **kwargs):
    return {"i": 0, "id": identifier, "letAgreed": False, "title": "1 Bed Flat, Example Road, W1",
            "description": "Bright self-contained flat", "imageUrl": "//imagescdn.openrent.co.uk/listings/700001/photo.JPG_homepage.JPG",
            "details": ["1 Bed", "1 Bath", "Furnished"], "rentPerMonth": 1300.0, "rentPerWeek": 300,
            "lastUpdated": "around 2 days ago", "isNew": False, "isMultiRoom": False,
            "maxRoomRentPerMonth": 0, "maxRoomRentPerWeek": 0, "isShownAsFeatured": False,
            "featuredPhotoUrls": [], **kwargs}


def detail_html(identifier=700001, description="A bright flat.<br>Floor area: 42 sq m.", rent=1300,
                available="20 November, 2026"):
    return f"""<html><head><link rel="canonical"
        href="https://www.openrent.co.uk/property-to-rent/london/1-bed-flat-example-road-w1/{identifier}"></head>
        <body><a class="lightbox_item d-block" href="//imagescdn.openrent.co.uk/listings/{identifier}/full.JPG">
        <img src="//imagescdn.openrent.co.uk/listings/{identifier}/full.JPG"></a>
        <div id="descriptionText" class="description-text"><div><p>{description}</p></div>
        <button id="toggleDescriptionText"><span>Read more</span></button></div>
        <table><tr><td class="fw-medium">Deposit</td><td>&#xA3;1,450.00</td></tr>
        <tr><td class="fw-medium">Rent PCM</td><td>&#xA3;{rent:,}.00</td></tr>
        <tr><td>Bills Included</td><td><svg class="text-danger or-icon"><path d="M18 6"></path></svg></td></tr>
        <tr><td>Available From</td><td>{available}</td></tr></table></body></html>"""


class OpenRentTests(unittest.TestCase):
    def setUp(self):
        cache._data = {}

    def test_search_reads_full_arrays_not_just_html_cards(self):
        rows = [index_row(700001), index_row(700002, bills=1)]
        with patch.object(net, "page", side_effect=[index_html(rows), json.dumps([card(700001), card(700002)])]) as page:
            homes = openrent.search()
        self.assertEqual([home.id for home in homes], ["700002", "700001"])
        self.assertEqual(page.call_args_list[0].kwargs["params"],
                         {"term": "Oxford Circus", "latn": 51.5152, "lngn": -0.1419, "area": 9,
                          "searchType": "km", "prices_max": 1600, "bedrooms_min": 1})
        self.assertEqual(page.call_args_list[1].kwargs["params"], [("ids", 700002), ("ids", 700001)])
        home = homes[0]
        self.assertEqual((home.key, home.url, home.rent, home.bedrooms, home.lat, home.lng, home.bills),
                         ("orent:700002", "https://www.openrent.co.uk/700002", 1300, 1, 51.52, -0.15, 0))
        self.assertEqual(home.address, "Example Road, W1")
        self.assertEqual(homes[1].bills, config.RENT_BILLS_ESTIMATE)
        self.assertTrue(home.image.startswith("https://imagescdn.openrent.co.uk/"))

    def test_filters_before_metadata_requests_and_respects_exclusive_access(self):
        variants = [{"prices": 1700}, {"prices": 900}, {"bedrooms": 0}, {"isshared": 1},
                    {"isstudio": 1}, {"nonStudents": 0}, {"islivelistBool": 0}, {"propertyTypes": 3},
                    {"PROPERTYLISTLATITUDES": 51.8}, {"dateFirstListedMs": 9999999999999}]
        rows = [index_row()] + [index_row(700002 + i, **values) for i, values in enumerate(variants)]
        with patch.object(net, "page", side_effect=[index_html(rows), json.dumps([card()])]) as page:
            self.assertEqual(len(openrent.search()), 1)
        self.assertEqual(page.call_args.kwargs["params"], [("ids", 700001)])

    def test_card_exclusions_even_if_index_is_misclassified(self):
        titles = ["Studio Flat, Road, W1", "Room in a Shared Flat, Road, W1",
                  "1 Bed Flat, Road, W1 - students only", "1 Bed Retirement Flat, Road, W1"]
        for title in titles:
            with self.subTest(title=title):
                self.assertIsNone(openrent._listing(card(title=title), index_row()))
        self.assertIsNone(openrent._listing(card(isMultiRoom=True), index_row()))
        self.assertIsNone(openrent._listing(card(letAgreed=True), index_row()))

    def test_batches_are_twenty_newest_first_and_bound_is_reported(self):
        rows = [index_row(700001 + i) for i in range(23)]
        calls = []

        def page(url, params):
            calls.append(params)
            if "propertiesbyid" not in url:
                return index_html(rows)
            return json.dumps([card(identifier) for _, identifier in reversed(params)])

        with patch.object(net, "page", side_effect=page), patch.object(config, "RENT_MAX_PAGES", 2):
            homes = openrent.search()
        self.assertEqual(len(homes), 23)
        self.assertEqual([len(params) for params in calls[1:]], [20, 3])
        self.assertEqual([h.id for h in homes], [str(700001 + i) for i in reversed(range(23))])
        with patch.object(net, "page", side_effect=page), patch.object(config, "RENT_MAX_PAGES", 1), \
                patch("sys.stderr", new_callable=io.StringIO) as error:
            homes = openrent.search()
        self.assertEqual(len(homes), 20)
        self.assertIn("inventory is incomplete", error.getvalue())

    def test_missing_schema_is_not_successful_empty_but_zero_results_are(self):
        for html in ("<html>Just a moment...</html>", index_html().replace("var prices", "var changed"),
                     index_html().replace("var bedrooms = [1,]", "var bedrooms = []"),
                     index_html().replace("var bills = [0,]", 'var bills = ["unknown",]')):
            with self.subTest(html=html[:40]), patch.object(net, "page", return_value=html):
                with self.assertRaises(ValueError):
                    openrent.search()
        with patch.object(net, "page", return_value=index_html([])) as page:
            self.assertEqual(openrent.search(), [])
            self.assertEqual(page.call_count, 1)

    def test_missing_or_wrong_metadata_id_fails_explicitly(self):
        for payload in ([], [card(700002)], [card(), card()], {"error": "blocked"},
                        [card(rentPerMonth=None)], [card(title="")]):
            with self.subTest(payload=payload), patch.object(net, "page", side_effect=[index_html(), json.dumps(payload)]):
                with self.assertRaises(ValueError):
                    openrent.search()

    def test_ignored_location_parameters_fail_instead_of_claiming_complete_results(self):
        html = index_html().replace("SEARCHRADIUS = 9", "SEARCHRADIUS = 1")
        with patch.object(net, "page", return_value=html):
            with self.assertRaisesRegex(ValueError, "does not cover"):
                openrent.search()

    def test_duplicate_index_ids_only_requested_once(self):
        with patch.object(net, "page", side_effect=[index_html([index_row(), index_row()]), json.dumps([card()])]) as page:
            self.assertEqual(len(openrent.search()), 1)
        self.assertEqual(page.call_args.kwargs["params"], [("ids", 700001)])

    def test_cache_minutes_expiry_and_filter_changes_refresh(self):
        home = openrent._listing(card(), index_row())
        with patch.object(openrent, "search", return_value=[home]) as search, \
                patch.object(config, "RENT_REFRESH_MINUTES", 10, create=True):
            openrent.fetch()
            self.assertEqual(openrent.fetch()[0].key, home.key)
            self.assertEqual(search.call_count, 1)
            with patch.object(config, "MAX_MONTHLY_COST", 1500):
                openrent.fetch()
            self.assertEqual(search.call_count, 2)
            cache._data["rent_search"]["orent"]["at"] = (
                dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=11)).isoformat()
            with patch.object(config, "MAX_MONTHLY_COST", 1500):
                openrent.fetch()
            self.assertEqual(search.call_count, 3)

    def test_legacy_cache_refresh_hours_still_supported(self):
        with patch.object(config, "RENT_REFRESH_HOURS", 2):
            search = Mock(return_value=[])
            cached_search("test-orent", search)
            cached_search("test-orent", search)
            self.assertEqual(search.call_count, 1)

    def test_refine_reads_nested_description_deposit_availability_full_photos_and_cache(self):
        home = openrent._listing(card(), index_row())
        with patch.object(net, "page", return_value=detail_html()) as page:
            self.assertTrue(openrent.refine(home))
            second = openrent._listing(card(), index_row())
            self.assertTrue(openrent.refine(second))
        self.assertEqual(page.call_count, 1)
        self.assertEqual((home.min_deposit, home.available, home.floor_area_m2), (1450, "2026-11-20", 42))
        self.assertEqual(home.image, "https://imagescdn.openrent.co.uk/listings/700001/full.JPG")
        self.assertEqual(home.bills, config.RENT_BILLS_ESTIMATE)
        cache._data["orent_detail"]["700001"]["at"] = "2000-01-01T00:00:00+00:00"
        with patch.object(net, "page", return_value=detail_html(rent=1400)) as page:
            self.assertTrue(openrent.refine(second))
        self.assertEqual((page.call_count, second.rent), (1, 1400))

    def test_details_reject_shared_students_retirement_small_flat_and_price_increase(self):
        for description in ("Flat with shared kitchen", "Students only", "Retirement accommodation",
                            "Floor area: 18 sq m."):
            cache._data = {}
            home = openrent._listing(card(), index_row())
            with self.subTest(description=description), patch.object(net, "page", return_value=detail_html(description=description)):
                self.assertFalse(openrent.refine(home))
        cache._data = {}
        with patch.object(net, "page", return_value=detail_html(rent=1800)):
            self.assertFalse(openrent.refine(openrent._listing(card(), index_row())))

    def test_refine_failure_does_not_cache_success_and_bills_are_authoritative(self):
        for html in ("<html>challenge</html>", detail_html(identifier=999), detail_html().replace("descriptionText", "missing"),
                     detail_html().replace("Rent PCM", "Rent changed")):
            with self.subTest(html=html[:30]), patch.object(net, "page", return_value=html):
                self.assertFalse(openrent.refine(openrent._listing(card(), index_row())))
            self.assertIsNone(cache.get("orent_detail", "700001"))
        with patch.object(net, "page", return_value=detail_html()):
            home = openrent._listing(card(), index_row(bills=1))
            self.assertTrue(openrent.refine(home))
            self.assertEqual(home.bills, config.RENT_BILLS_ESTIMATE)


if __name__ == "__main__":
    unittest.main()
