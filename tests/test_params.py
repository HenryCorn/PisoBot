"""Per-person search preferences: studios, furnished, maxwalk, maxchanges, areas. Fixtures only, no network."""
import json
import unittest
from unittest.mock import Mock, patch

try:
    from tests.test_users import Base, FRIEND, OWNER, message
except ImportError:      # run as `python -m unittest discover -s tests`
    from test_users import Base, FRIEND, OWNER, message

from pisobot import bot, config, d1, enrich, main, models, net, rental_text, telegram, users, watch
from pisobot.models import Listing
from pisobot.sources import (PRIVATE_RENT, apply_rent_details, min_bedrooms, openrent, otm_rent, private_rent_costs,
                             rental_filters, rightmove, spareroom, studios_wanted)


def flat(i=1, description="Bright self-contained flat", **kw):
    values = dict(source="rm", id=str(i), url=f"https://example.com/{i}", address="Home, London E5", bedrooms=1,
                  scheme=PRIVATE_RENT, tenure="rent", rent=1400, lat=51.52, lng=-0.14, description=description)
    values.update(kw)
    return private_rent_costs(Listing(**values))


def next_data(page_props):
    return '<script id="__NEXT_DATA__">' + json.dumps({"props": {"pageProps": page_props}}) + "</script>"


def rm_property(id, **kw):
    return {"id": id, "bedrooms": 0, "displayAddress": "Warwick Avenue W9", "propertySubType": "Flat",
            "summary": "Bright", "location": {"latitude": 51.52, "longitude": -0.18},
            "price": {"amount": 1300, "frequency": "monthly"}, **kw}


def otm_card(id="1", **kw):
    return {"id": id, "property-title": "1 bedroom flat to rent", "humanised-property-type": "Flat",
            "price": "£1,400 pcm", "bedrooms": 1, "address": "Home SE22", "location": {"lat": 51.4, "lon": -0.07}, **kw}


STUDIO_ON = {"studios": 0.0}


class ParseAndDescribeTests(unittest.TestCase):
    def test_studios_accepts_on_or_a_sensible_area(self):
        self.assertEqual(users.parse("studios", "on"), 0.0)
        self.assertEqual(users.parse("studios", "yes"), 0.0)
        self.assertEqual(users.parse("studios", "28"), 28.0)
        self.assertEqual(users.parse("studios", "28 m2"), 28.0)
        for bad in ("5", "500", "big", "nan"):
            with self.assertRaises(ValueError):
                users.parse("studios", bad)

    def test_furnished_is_one_of_three_words(self):
        self.assertEqual(users.parse("furnished", " Unfurnished "), "unfurnished")
        self.assertEqual(users.parse("furnished", "any"), "any")
        with self.assertRaisesRegex(ValueError, "one of any, furnished, unfurnished"):
            users.parse("furnished", "sofa")

    def test_walk_and_changes_are_bounded_whole_numbers(self):
        self.assertEqual(users.parse("maxwalk", "12 min"), 12)
        self.assertEqual(users.parse("maxchanges", "0"), 0)
        self.assertEqual(users.parse("maxchanges", "2"), 2)
        for name, bad in (("maxwalk", "0"), ("maxwalk", "90"), ("maxchanges", "9"), ("maxchanges", "-1"),
                          ("maxwalk", "far")):
            with self.assertRaises(ValueError):
                users.parse(name, bad)

    def test_areas_are_checked_against_the_district_table(self):
        self.assertEqual(users.parse_areas("exclude n7, SE15 e8"), {"exclude": ["E8", "N7", "SE15"]})
        self.assertEqual(users.parse_areas("include E5", {"exclude": ["N7"]}), {"exclude": ["N7"], "include": ["E5"]})
        self.assertEqual(users.parse_areas("exclude none", {"exclude": ["N7"]}), {})
        self.assertEqual(users.parse_areas("include N1C"), {"include": ["N1C"]})
        for raw, text in (("exclude Z99 N7", "Z99"), ("exclude", "at least one district"), ("N7", "exclude"),
                          ("exclude XX1", "XX1")):
            with self.assertRaisesRegex(ValueError, text):
                users.parse_areas(raw)
        with self.assertRaisesRegex(ValueError, "both"):
            users.parse_areas("include N7", {"exclude": ["N7"]})
        self.assertTrue(all(models.normalise_district(c) for c in ("N7", "SE15", "W1", "NW10")))
        self.assertIsNone(models.normalise_district("Q1"))

    def test_describe_shows_defaults_and_choices(self):
        text = users.describe(users.User("1"))
        for part in ("<b>studios</b> off <i>(default)</i>", "<b>furnished</b> any <i>(default)</i>",
                     "<b>maxwalk</b> off <i>(default)</i>", "<b>maxchanges</b> off <i>(default)</i>",
                     "<b>areas</b> all of London <i>(default)</i>"):
            self.assertIn(part, text)
        text = users.describe(users.User("1", params={
            "studios": 28.0, "furnished": "unfurnished", "maxwalk": 10, "maxchanges": 0,
            "areas": {"exclude": ["N7"], "include": ["E5", "E8"]}}))
        for part in ("<b>studios</b> on, at least 28 m²", "<b>furnished</b> unfurnished", "<b>maxwalk</b> 10 min",
                     "<b>maxchanges</b> 0", "<b>areas</b> exclude N7; include E5 E8"):
            self.assertIn(part, text)
        self.assertIn("<b>studios</b> on (any size)", users.describe(users.User("1", params=STUDIO_ON)))


class TwoPeopleTests(unittest.TestCase):
    def test_criteria_apply_per_person_and_are_restored(self):
        a = users.User("1", params={"studios": 28.0, "maxwalk": 10, "furnished": "furnished",
                                    "areas": {"exclude": ["N7"]}})
        b = users.User("2", params={"maxchanges": 1, "areas": {"include": ["E5"]}})
        with users.criteria(a.params):
            seen_a = (config.STUDIO_MIN_AREA, config.MAX_WALK_MINUTES, config.MAX_CHANGES, config.FURNISHED,
                      config.AREAS_EXCLUDE, config.AREAS_INCLUDE)
            with users.criteria(b.params):
                seen_b = (config.STUDIO_MIN_AREA, config.MAX_WALK_MINUTES, config.MAX_CHANGES, config.FURNISHED,
                          config.AREAS_EXCLUDE, config.AREAS_INCLUDE)
        self.assertEqual(seen_a, (28.0, 10, None, "furnished", ("N7",), ()))
        # criteria() overrides only what a person set; an areas setting replaces both lists
        self.assertEqual(seen_b, (28.0, 10, 1, "furnished", (), ("E5",)))
        with users.criteria(b.params):
            self.assertEqual((config.STUDIO_MIN_AREA, config.MAX_WALK_MINUTES, config.FURNISHED), (None, None, "any"))
        self.assertEqual((config.STUDIO_MIN_AREA, config.MAX_WALK_MINUTES, config.MAX_CHANGES, config.FURNISHED,
                          config.AREAS_EXCLUDE, config.AREAS_INCLUDE), (None, None, None, "any", (), ()))

    def test_a_studio_search_is_separate_but_minimum_areas_and_other_preferences_share(self):
        want = users.User("1", params={"studios": 28.0})
        want_more = users.User("2", params={"studios": 40.0, "furnished": "furnished", "maxwalk": 5})
        refuse = users.User("3", params={"maxchanges": 1})
        groups = users.search_groups([want, refuse, want_more])
        self.assertEqual([[p.chat_id for p in people] for _, people in groups], [["1", "2"], ["3"]])
        self.assertEqual([params["studios"] for params, _ in groups], [0, None])
        self.assertEqual(groups[0][0]["beds"], 1)
        self.assertEqual(len(users.search_groups([users.User("1"), users.User("2")])), 1)

    def test_fetch_criteria_make_the_adapters_keep_studios_for_that_group_only(self):
        want, refuse = users.User("1", params=STUDIO_ON), users.User("2")
        with users.criteria(users.fetch_params(want)):
            self.assertTrue(studios_wanted())
            self.assertEqual(min_bedrooms(), 0)
        with users.criteria(users.fetch_params(refuse)):
            self.assertFalse(studios_wanted())
            self.assertEqual(min_bedrooms(), 1)
        with users.criteria({**users.fetch_params(want), "beds": 2}):
            self.assertEqual(min_bedrooms(), 2)             # a person who needs two beds gets no studios
        self.assertNotEqual(rental_filters(), None)
        with users.criteria(users.fetch_params(want)):
            on = rental_filters()
        self.assertNotEqual(on, rental_filters())              # cache keys differ, so groups never share a cache


class AdapterTests(unittest.TestCase):
    def test_default_queries_and_results_are_unchanged(self):
        self.assertIsNone(config.STUDIO_MIN_AREA)
        with patch.object(net, "page", return_value=next_data({"searchResults": {"properties": [], "resultCount": "0"}})) as get:
            rightmove.search()
        self.assertEqual(get.call_args.kwargs["params"]["minBedrooms"], 1)
        html = next_data({"initialReduxState": {"results": {"list": [], "paginationControls": {}}}})
        with patch.object(net, "page", return_value=html) as get:
            otm_rent.search()
        self.assertEqual(get.call_args.kwargs["params"]["min-bedrooms"], 1)
        self.assertIsNone(rightmove._listing(rm_property(1, propertySubType="Studio")))
        self.assertIsNone(otm_rent._listing(otm_card(**{"humanised-property-type": "Studio"})))
        self.assertIsNone(otm_rent._listing(otm_card(**{"property-title": "Studio flat to rent"})))

    def test_rightmove_keeps_and_tags_studios_only_when_wanted(self):
        with users.criteria(STUDIO_ON):
            studio = rightmove._listing(rm_property(2, propertySubType="Studio Flat"))
            flat_ = rightmove._listing(rm_property(3, bedrooms=1))
            self.assertIsNone(rightmove._listing(rm_property(4, propertySubType="House Share")))
            html = next_data({"searchResults": {"properties": [], "resultCount": "0"}})
            with patch.object(net, "page", return_value=html) as get:
                rightmove.search()
        self.assertTrue(studio.studio)
        self.assertFalse(flat_.studio)
        self.assertEqual(get.call_args.kwargs["params"]["minBedrooms"], 0)

    def test_onthemarket_keeps_and_tags_studios_only_when_wanted(self):
        with users.criteria(STUDIO_ON):
            by_type = otm_rent._listing(otm_card("1", **{"humanised-property-type": "Studio", "bedrooms": 0}))
            by_title = otm_rent._listing(otm_card("2", **{"property-title": "Studio flat to rent"}))
            plain = otm_rent._listing(otm_card("3"))
            self.assertIsNone(otm_rent._listing(otm_card("4", **{"property-title": "Room to rent"})))
            html = next_data({"initialReduxState": {"results": {"list": [], "paginationControls": {}}}})
            with patch.object(net, "page", return_value=html) as get:
                otm_rent.search()
        self.assertEqual((by_type.studio, by_title.studio, plain.studio), (True, True, False))
        self.assertEqual(get.call_args.kwargs["params"]["min-bedrooms"], 0)

    def test_openrent_index_and_card(self):
        try:
            from tests.test_openrent import card, index_html, index_row
        except ImportError:
            from test_openrent import card, index_html, index_row
        rows = [index_row(700001), index_row(700002, isstudio=1, bedrooms=0)]
        with patch.object(openrent.time, "time", return_value=1700000000 + 10**7):
            self.assertEqual([r["PROPERTYIDS"] for r in openrent._index(index_html(rows))], [700001])
            with users.criteria(STUDIO_ON):
                self.assertEqual(sorted(r["PROPERTYIDS"] for r in openrent._index(index_html(rows))), [700001, 700002])
        with users.criteria(STUDIO_ON):
            studio = openrent._listing(card(700002, title="Studio, Example Road, W1"), rows[1])
            self.assertTrue(studio.studio)
            self.assertEqual(studio.bedrooms, 0)
            self.assertFalse(openrent._listing(card(), rows[0]).studio)
        with self.assertRaises(ValueError):                    # no "N Bed" and not flagged a studio
            openrent._listing(card(700003, title="Lovely place, W1"), index_row(700003))

    def test_spareroom_keeps_studios_only_when_wanted(self):
        template = ('<li class="listing-result" data-listing-id="{id}" data-listing-type="flatstorent" '
                    'data-listing-title="{title}" data-listing-postcode="W1" data-listing-property-type="{kind}" '
                    'data-listing-rooms-in-property="{beds}" data-listing-ad-headline-rate="&pound;1,300" '
                    'data-listing-ad-headline-rate-period="pcm"></li>')
        cards = "".join(template.format(**c) for c in (
            dict(id=1, title="Nice flat", kind="flat", beds=1),
            dict(id=2, title="Studio near park", kind="studio", beds=0),
            dict(id=3, title="Nice bedsit", kind="flat", beds=1)))
        with patch.object(spareroom.net, "page", return_value=cards), patch.object(spareroom, "_coords",
                                                                                   return_value=(51.52, -0.13)), \
                patch.object(config, "MAX_RENT", 3000):
            self.assertEqual([l.id for l in spareroom.search()], ["1"])
            with users.criteria(STUDIO_ON):
                found = {l.id: l for l in spareroom.search()}
        self.assertEqual(sorted(found), ["1", "2"])
        self.assertTrue(found["2"].studio)
        self.assertFalse(found["1"].studio)

    def test_a_small_studio_is_not_dropped_as_a_room_by_the_generic_area_rule(self):
        small = flat(2, "Studio flat", studio=True, floor_area_m2=22)
        self.assertFalse(apply_rent_details(small, {"description": "x"}))
        with users.criteria(STUDIO_ON):
            self.assertTrue(apply_rent_details(small, {"description": "x"}))
            tiny_flat = flat(3, "One bed flat", floor_area_m2=22)
            self.assertFalse(apply_rent_details(tiny_flat, {"description": "x"}))

    def test_rightmove_details_expose_furnishing(self):
        for text, expected in (("Furnished", "furnished"), ("Unfurnished", "unfurnished"), ("Part furnished", "part"),
                               ("Furnished or unfurnished, landlord is flexible", "flexible"), ("", None)):
            self.assertEqual(rightmove._details({"lettings": {"furnishType": text}})["furnishing"], expected, text)
        listing = flat(5)
        apply_rent_details(listing, {"description": "x", "furnishing": "unfurnished"})
        self.assertEqual(listing.furnishing, "unfurnished")


class GateTests(unittest.TestCase):
    def setUp(self):
        for target in (patch.object(rightmove, "refine", return_value=True),
                       patch.object(enrich, "distance_km", return_value=3.0)):
            target.start()
            self.addCleanup(target.stop)

    def basic(self, listing, params=None):
        with users.criteria(params or {}):
            return main.passes_basic(listing)

    def test_studios_off_is_todays_behaviour_with_its_own_reason(self):
        self.assertEqual(self.basic(flat(1, "Modern studio apartment", studio=True, bedrooms=0)), "studio")
        self.assertEqual(self.basic(flat(2, "1 bedroom flat to rent. Studio Flat Wooden Flooring")), "studio")
        self.assertIsNone(self.basic(flat(3)))
        self.assertIsNone(self.basic(flat(4, "A spacious one bed flat with a studio style kitchen")))
        sale = Listing("stb", "9", "u", "a", 1, "Shared Ownership", tenure="sale", share_price=100000)
        self.assertFalse(sale.studio)

    def test_studios_on_let_a_studio_through_as_one_room_and_label_it(self):
        studio = flat(5, "Modern studio apartment", studio=True, bedrooms=0)
        self.assertIsNone(self.basic(studio, STUDIO_ON))
        card = telegram.facts_line(studio)
        self.assertIn("<b>Studio</b>", card)
        self.assertNotIn("0 bed", card)
        self.assertIn("1 bed", telegram.facts_line(flat(6)))
        untagged = flat(7, "1 bedroom flat to rent. Studio Flat Wooden Flooring")
        self.assertIsNone(self.basic(untagged, STUDIO_ON))
        self.assertTrue(untagged.studio)                         # recognised from the text and labelled
        self.assertEqual(self.basic(flat(8, "Modern studio apartment", studio=True, bedrooms=0),
                                    {**STUDIO_ON, "beds": 2}), "beds")

    def test_studio_still_loses_to_the_other_keyword_rules(self):
        self.assertEqual(self.basic(flat(9, "Studio flat. Room to rent in a shared house", studio=True), STUDIO_ON),
                         "keywords")
        self.assertEqual(self.basic(flat(10, "Studio apartment, students only", studio=True), STUDIO_ON), "keywords")

    def test_minimum_studio_area_needs_a_published_area(self):
        params = {"studios": 28.0}
        self.assertEqual(self.basic(flat(11, "Studio flat", studio=True), params), "studio_area")           # none published
        self.assertEqual(self.basic(flat(12, "Studio flat", studio=True, floor_area_m2=25), params), "studio_area")
        self.assertIsNone(self.basic(flat(13, "Studio flat", studio=True, floor_area_m2=28), params))
        self.assertIsNone(self.basic(flat(14, "Studio flat", studio=True, floor_area_m2=41), params))
        self.assertIsNone(self.basic(flat(15, "One bed flat", floor_area_m2=20), params))                     # not a studio
        # the area can come out of the description text after the details page
        self.assertIsNone(self.basic(flat(16, "Studio flat. Floor area: 33 sq m.", studio=True), params))

    def test_furnished_rejects_only_a_stated_opposite(self):
        for want, text, expected in (
                ("furnished", "Unfurnished flat", "furnishing"), ("furnished", "Fully furnished", None),
                ("furnished", "Furnished or unfurnished", None), ("furnished", "Part furnished", None),
                ("furnished", "Nice flat, no mention", None), ("unfurnished", "Fully furnished", "furnishing"),
                ("unfurnished", "Unfurnished", None), ("unfurnished", "Nice flat, no mention", None),
                ("any", "Unfurnished flat", None)):
            with self.subTest(want=want, text=text):
                self.assertEqual(self.basic(flat(20, text), {"furnished": want}), expected)
        structured = flat(21, "Nice flat", furnishing="unfurnished")
        self.assertEqual(self.basic(structured, {"furnished": "furnished"}), "furnishing")

    def test_card_says_when_furnishing_is_not_stated(self):
        silent, stated = flat(22, "Nice flat"), flat(23, "Nice flat", furnishing="furnished")
        with users.criteria({"furnished": "furnished"}):
            self.assertIn("ℹ️ Furnishing not stated", silent.feature_tags)
            self.assertNotIn("ℹ️ Furnishing not stated", stated.feature_tags)
        self.assertNotIn("ℹ️ Furnishing not stated", silent.feature_tags)       # nobody asked

    def test_areas_exclude_and_include(self):
        e5, n7 = flat(30, address="Home, London E5"), flat(31, address="Home, London N7 6AA")
        self.assertIsNone(self.basic(n7))
        self.assertEqual(self.basic(n7, {"areas": {"exclude": ["N7", "SE15"]}}), "area_excluded")
        self.assertIsNone(self.basic(e5, {"areas": {"exclude": ["N7"]}}))
        self.assertIsNone(self.basic(e5, {"areas": {"include": ["E5", "E8"]}}))
        self.assertEqual(self.basic(n7, {"areas": {"include": ["E5", "E8"]}}), "area_outside")
        nowhere = flat(32, address="Somewhere, London")
        self.assertEqual(self.basic(nowhere, {"areas": {"include": ["E5"]}}), "area_outside")
        self.assertIsNone(self.basic(nowhere, {"areas": {"exclude": ["E5"]}}))

    def test_sub_districts_follow_their_parent_unless_they_are_districts_themselves(self):
        self.assertTrue(models.in_districts("W1K", ("W1",)))
        self.assertTrue(models.in_districts("N1C", ("N1C",)))
        self.assertFalse(models.in_districts("N1C", ("N1",)))          # N1C is its own district in the table
        self.assertFalse(models.in_districts("", ("N1",)))
        self.assertFalse(models.in_districts("E5", ()))

    def area(self, minutes=25, walk=5, changes=0, params=None):
        journey = {"minutes": minutes, "route": "Tube", "station": "S", "walk": walk, "changes": changes}
        with patch.object(enrich, "journey", return_value=journey), \
                patch.object(enrich, "crime_score", return_value=30) as crime, users.criteria(params or {}):
            return main.passes_area(flat(40)), crime

    def test_walk_and_changes_limits_use_the_journey_already_fetched(self):
        self.assertEqual(self.area(walk=25, changes=3)[0], None)                       # off by default
        why, crime = self.area(walk=12, params={"maxwalk": 10})
        self.assertEqual(why, "walk")
        crime.assert_not_called()                                                    # no further lookup spent
        self.assertIsNone(self.area(walk=10, params={"maxwalk": 10})[0])
        self.assertEqual(self.area(changes=2, params={"maxchanges": 1})[0], "changes")
        self.assertIsNone(self.area(changes=1, params={"maxchanges": 1})[0])
        self.assertEqual(self.area(changes=1, params={"maxchanges": 0})[0], "changes")
        self.assertIsNone(self.area(changes=0, params={"maxchanges": 0})[0])
        self.assertEqual(self.area(minutes=70, params={"maxwalk": 10})[0], "travel")     # commute limit still first

    def test_default_gates_do_not_change_a_normal_flat(self):
        self.assertIsNone(self.basic(flat(50)))
        why, _ = self.area()
        self.assertIsNone(why)


class FunnelAndSettingsTests(Base):
    def setUp(self):
        super().setUp()
        for target in (
            patch.object(rightmove, "refine", return_value=True),
            patch.object(main.enrich, "geocode"), patch.object(main.enrich, "assign_crime_percentiles"),
            patch.object(main.enrich, "crime_score", return_value=30),
            patch.object(main.enrich, "journey", side_effect=self.journey),
            patch.object(main, "_ensure_photos"), patch.object(telegram, "send_text"),
            patch.object(telegram, "send_listing"), patch.object(telegram, "throttle"),
        ):
            target.start()
            self.addCleanup(target.stop)

    @staticmethod
    def journey(lat, lng):
        walk = 12 if abs(lat - 51.5301) < 1e-6 else 3
        changes = 2 if abs(lat - 51.5302) < 1e-6 else 0
        return {"minutes": 25, "route": "Tube", "station": "S", "walk": walk, "changes": changes}

    def funnel(self, listings, params):
        with users.criteria(params):
            main._run(["rm"], False, False, rentals=True, watch=True, baseline=True, prefetched=(listings, []))
        return json.loads(d1.query("SELECT stats FROM rental_funnel ORDER BY id DESC LIMIT 1")[0]["stats"])[
            "sources"]["rm"]

    def test_each_new_rejection_has_its_own_funnel_reason(self):
        studio = flat(1, "Modern studio apartment", studio=True, bedrooms=0)
        normal = flat(2)
        n7 = flat(3, address="Home, London N7 6AA")
        stats = self.funnel([studio, normal, n7], {"areas": {"exclude": ["N7"]}})
        self.assertEqual((stats["rejected"], stats["matched"]), ({"studio": 1, "area_excluded": 1}, 1))
        stats = self.funnel([normal, n7], {"areas": {"include": ["E5"]}})
        self.assertEqual((stats["rejected"], stats["matched"]), ({"area_outside": 1}, 1))
        stats = self.funnel([flat(4, "Studio flat", studio=True, bedrooms=0)], {"studios": 28.0})
        self.assertEqual(stats["rejected"], {"studio_area": 1})
        listings = [flat(5, "Unfurnished flat"), flat(6, lat=51.5301), flat(7, lat=51.5302), flat(8)]
        stats = self.funnel(listings, {"furnished": "furnished", "maxwalk": 10, "maxchanges": 1})
        self.assertEqual(stats["rejected"], {"furnishing": 1, "walk": 1, "changes": 1})
        self.assertEqual(stats["matched"], 1)

    def test_a_studio_group_and_a_normal_group_each_keep_their_own_adverts(self):
        studio, normal = flat(1, "Modern studio apartment", studio=True, bedrooms=0), flat(2)
        wanted = self.funnel([studio, normal], STUDIO_ON)
        refused = self.funnel([studio, normal], {})
        self.assertEqual((wanted["matched"], wanted["rejected"]), (2, {}))
        self.assertEqual((refused["matched"], refused["rejected"]), (1, {"studio": 1}))


class SettingsCommandTests(Base):
    def setUp(self):
        super().setUp()
        self.client = Mock()
        self.handler = bot.Handler(OWNER, Mock(return_value=True), self.client)

    def say(self, text, chat=FRIEND):
        self.handler.handle(message(text, chat))
        return [c.kwargs["text"] for c in self.client.call.call_args_list if c.args[0] == "sendMessage"][-1]

    def test_set_each_preference_validate_and_turn_off(self):
        self.admit()
        for text, expected in (("/set studios on", {"studios": 0.0}), ("/set studios 28", {"studios": 28.0}),
                               ("/set studios off", {}), ("/set furnished unfurnished", {"furnished": "unfurnished"}),
                               ("/set furnished default", {}), ("/set maxwalk 12", {"maxwalk": 12}),
                               ("/set maxwalk off", {}), ("/set maxchanges 0", {"maxchanges": 0}),
                               ("/set maxchanges none", {}),
                               ("/set areas exclude N7 SE15", {"areas": {"exclude": ["N7", "SE15"]}}),
                               ("/set areas include E5", {"areas": {"exclude": ["N7", "SE15"], "include": ["E5"]}}),
                               ("/set areas exclude none", {"areas": {"include": ["E5"]}}),
                               ("/set areas default", {})):
            reply = self.say(text)
            self.assertIn("Search updated", reply, text)
            self.assertEqual(users.get(FRIEND).params, expected, text)
        for text, fragment in (("/set studios 5", "between 10 and 200"), ("/set furnished sofa", "one of any"),
                               ("/set maxwalk 0", "between 1 and 60"), ("/set maxchanges 9", "between 0 and 5"),
                               ("/set areas exclude Z99", "Z99"), ("/set areas up N7", "exclude"),
                               ("/set areas", "Usage")):
            self.assertIn(fragment, self.say(text), text)
        self.assertEqual(users.get(FRIEND).params, {})

    def test_settings_lists_and_explains_the_preferences(self):
        self.admit({"studios": 30.0, "maxchanges": 0, "areas": {"exclude": ["N7"]}})
        text = self.say("/settings")
        for part in ("<b>studios</b> on, at least 30 m²", "<b>maxchanges</b> 0", "<b>maxwalk</b> off",
                     "<b>furnished</b> any", "<b>areas</b> exclude N7", "Preferences", "furnishing not stated"):
            self.assertIn(part, text)

    def test_two_people_have_different_preferences(self):
        self.admit()
        self.say("/set studios 28", FRIEND)
        self.say("/set areas exclude N7", OWNER)
        self.say("/set maxwalk 8", OWNER)
        friend, owner = users.get(FRIEND), users.owner()
        self.assertEqual(friend.params, {"studios": 28.0})
        self.assertEqual(owner.params, {"areas": {"exclude": ["N7"]}, "maxwalk": 8})
        with users.criteria(friend.params):
            self.assertEqual((config.STUDIO_MIN_AREA, config.AREAS_EXCLUDE, config.MAX_WALK_MINUTES), (28.0, (), None))
        with users.criteria(owner.params):
            self.assertEqual((config.STUDIO_MIN_AREA, config.AREAS_EXCLUDE, config.MAX_WALK_MINUTES), (None, ("N7",), 8))

    def test_changing_a_preference_clears_that_persons_baseline_marker(self):
        friend = self.admit()
        for text in ("/set studios on", "/set furnished furnished", "/set maxwalk 9", "/set maxchanges 1",
                     "/set areas exclude N7"):
            with users.use(friend):
                watch._put_meta("watch_initialized", "1")
            self.say(text)
            with users.use(friend):
                self.assertIsNone(watch._meta("watch_initialized"), text)

    def test_the_settings_fingerprint_rebaselines_quietly_for_every_new_preference(self):
        person = users.owner()
        calls = []

        def fake_run(*args, **kwargs):
            calls.append(kwargs["baseline"])
            return {"matches": 0, "complete": True, "failures": [], "enrich_failures": 0}

        def cycle():
            with patch.object(main, "_run", side_effect=fake_run), users.use(users.owner()):
                watch.Watcher(["rm"])._person_cycle("rentals", ([], []), None, False)
            return calls[-1]

        self.assertTrue(cycle())                                      # first ever: baseline
        self.assertFalse(cycle())
        for params in ({"studios": 0.0}, {"studios": 28.0}, {"studios": 28.0, "furnished": "furnished"},
                       {"furnished": "unfurnished", "maxwalk": 10}, {"maxwalk": 10, "maxchanges": 0},
                       {"maxchanges": 1, "areas": {"exclude": ["N7"]}}, {"areas": {"include": ["E5"]}}, {}):
            users.save_params(person, params)
            self.assertTrue(cycle(), params)                          # a change: quiet re-baseline, no alerts
            self.assertFalse(cycle(), params)                         # and then normal


class CloudUnchangedTests(unittest.TestCase):
    def test_defaults_are_todays_behaviour(self):
        self.assertEqual((config.STUDIO_MIN_AREA, config.FURNISHED, config.MAX_WALK_MINUTES, config.MAX_CHANGES,
                          config.AREAS_EXCLUDE, config.AREAS_INCLUDE), (None, "any", None, None, (), ()))
        self.assertEqual(rental_text.exclude_keywords(), config.EXCLUDE_KEYWORDS)
        with users.criteria(STUDIO_ON):
            self.assertNotIn("studio", rental_text.exclude_keywords())
            self.assertIn("room to rent", rental_text.exclude_keywords())
        sale = Listing("stb", "9", "u", "a", 1, "Shared Ownership", description="Studio flat")
        self.assertTrue(sale.excluded_by_keywords())                                  # sale rules untouched
        self.assertFalse(sale.studio)


if __name__ == "__main__":
    unittest.main()
