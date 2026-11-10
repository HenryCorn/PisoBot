"""signals.py: pure detectors, selection, card line and the hypothesis log. Fabricated history; no network."""
import datetime as dt
import os
import tempfile
import unittest
from unittest.mock import patch

from pisobot import signals
from pisobot.models import Listing

TODAY = dt.date(2026, 11, 10)


def flat(i=1, rent=1500, beds=1, lat=51.5, lng=-0.1, **kw):
    kw.setdefault("images", [f"https://m/{i}_{n}.jpg" for n in range(8)])
    kw.setdefault("floorplan", "https://m/plan.png")
    kw.setdefault("description", "A bright one bedroom flat close to the station with a large living room. " * 6)
    return Listing(source="rm", id=str(i), url=f"https://rm/{i}", address="Road, London N7", bedrooms=beds,
                   scheme="Private rent", tenure="rent", rent=rent, lat=lat, lng=lng, **kw)


class UnderPresentedTests(unittest.TestCase):
    def test_well_presented_flat_is_quiet(self):
        self.assertIsNone(signals.under_presented(flat()))

    def test_one_shortfall_is_not_enough(self):
        self.assertIsNone(signals.under_presented(flat(floorplan=None)))

    def test_two_shortfalls(self):
        s = signals.under_presented(flat(floorplan=None, images=["a", "b"]))
        self.assertEqual((s.name, s.strength), ("under_presented", 2))
        self.assertIn("2 photos", s.evidence)
        self.assertIn("no floorplan", s.evidence)

    def test_three_shortfalls_strong(self):
        s = signals.under_presented(flat(floorplan=None, images=["a"], description="Nice flat."))
        self.assertEqual(s.strength, 3)

    def test_studio_that_is_a_one_bed(self):
        s = signals.under_presented(flat(studio=True, floor_area_m2=44, description="Studio. One bedroom flat. " * 20))
        self.assertEqual(s.strength, 3)
        self.assertIn("called a studio", s.evidence)

    def test_small_studio_is_not_contradictory(self):
        self.assertIsNone(signals.under_presented(flat(studio=True, floor_area_m2=24, description="Studio flat. " * 30)))

    def test_two_bed_in_tiny_area(self):
        s = signals.under_presented(flat(beds=2, floor_area_m2=33))
        self.assertIn("2 bedrooms in only 33 m²", s.evidence)


class ReducedStaleTests(unittest.TestCase):
    def test_fresh_unreduced_flat_is_quiet(self):
        self.assertIsNone(signals.reduced_stale(flat(first_listed="2026-11-08"), today=TODAY))

    def test_portal_reduced(self):
        s = signals.reduced_stale(flat(update_reason="reduced", update_date="2026-11-09"), today=TODAY)
        self.assertIn("portal says reduced 1 days ago", s.evidence)
        self.assertIn("Idea, not advice", s.idea)
        self.assertIn("offers above asking are not allowed", s.idea)

    def test_stale_after_21_days(self):
        self.assertIsNone(signals.reduced_stale(flat(first_listed="2026-10-20"), today=TODAY))   # 21 days
        s = signals.reduced_stale(flat(first_listed="2026-10-19"), today=TODAY)               # 22 days
        self.assertEqual(s.label, "Long on the market")
        self.assertIn("22 days", s.evidence)

    def test_history_cuts_counted(self):
        rows = [{"at": "2026-10-01", "state": "listed", "rent": 1650}, {"at": "2026-10-15", "state": "listed", "rent": 1600},
                {"at": "2026-11-01", "state": "listed", "rent": 1500}]
        s = signals.reduced_stale(flat(rent=1500, first_listed="2026-10-01"), rows, TODAY)
        self.assertIn("2 cuts seen, £150 off in total (£1,650 to £1,500)", s.evidence)
        self.assertIn("5% under", s.idea)
        self.assertGreaterEqual(s.strength, 2)

    def test_idea_is_never_above_asking(self):
        s = signals.reduced_stale(flat(rent=1500, first_listed="2026-09-01"), today=TODAY)
        self.assertIn("about £1,420", s.idea)

    def test_no_rent_no_idea(self):
        l = flat(first_listed="2026-09-01")
        l.rent = None
        self.assertEqual(signals.reduced_stale(l, today=TODAY).idea, "")


class BackOnMarketTests(unittest.TestCase):
    def test_no_history_no_signal(self):
        self.assertIsNone(signals.back_on_market(flat(), [], TODAY))

    def test_was_let_agreed(self):
        rows = [{"at": "2026-10-20", "state": "listed"}, {"at": "2026-10-28", "state": "let_agreed"}]
        s = signals.back_on_market(flat(), rows, TODAY)
        self.assertEqual((s.name, s.strength), ("back_on_market", 3))
        self.assertIn("a deal may have fallen through", s.evidence)

    def test_gone_for_days_then_back(self):
        rows = [{"at": "2026-10-20", "state": "listed"}, {"at": "2026-11-01", "state": "gone"}]
        self.assertEqual(signals.back_on_market(flat(), rows, TODAY).strength, 2)

    def test_gone_yesterday_is_a_blip_not_a_return(self):
        self.assertIsNone(signals.back_on_market(flat(), [{"at": "2026-11-09", "state": "gone"}], TODAY))

    def test_old_marks_are_not_news(self):
        self.assertIsNone(signals.back_on_market(flat(), [{"at": "2026-08-01", "state": "let_agreed"}], TODAY))

    def test_history_provider_and_failure(self):
        l = flat()
        rows = {l.key: [{"at": "2026-10-28", "state": "let_agreed"}]}
        self.assertEqual(signals.compute(l, [l], rows.get, TODAY)[0].name, "back_on_market")

        def broken(key):
            raise RuntimeError("archive down")
        self.assertEqual(signals.compute(flat(), [], broken, TODAY), [])


class SameFlatTests(unittest.TestCase):
    def test_shared_photo_points_to_cheaper(self):
        a = flat(1, rent=1600, images=["https://a/x/p1.jpg", "https://a/x/p2.jpg"] + ["https://a/q%d.jpg" % i for i in range(4)])
        b = flat(2, rent=1500, images=["https://b/y/p1.jpg"] + ["https://b/r%d.jpg" % i for i in range(4)], agent="Other Agents")
        s = signals.same_flat_two_prices(a, [a, b])
        self.assertEqual((s.label, s.strength), ("Probably also listed £100/mo cheaper", 3))
        self.assertIn("Other Agents", s.evidence)
        self.assertIn("https://rm/2", s.evidence)
        t = signals.same_flat_two_prices(b, [a, b])
        self.assertEqual(t.label, "Cheapest of 2 adverts for this flat")

    def test_same_area_and_text_is_medium(self):
        text = "Spacious modern apartment with balcony and concierge near station"
        a = flat(1, rent=1600, floor_area_m2=50.0, description=text, images=["a1"])
        b = flat(2, rent=1500, floor_area_m2=50.2, description=text + " available now", images=["b1"])
        self.assertEqual(signals.same_flat_two_prices(a, [a, b]).strength, 2)

    def test_identical_units_with_different_text_are_not_the_same_flat(self):
        a = flat(1, rent=1600, floor_area_m2=50.0, description="Brand new tower block flat with gym", images=["a1"])
        b = flat(2, rent=1500, floor_area_m2=50.0, description="Victorian conversion garden views quiet", images=["b1"])
        self.assertIsNone(signals.same_flat_two_prices(a, [a, b]))

    def test_far_apart_other_beds_or_same_price_ignored(self):
        a = flat(1, rent=1600, images=["p.jpg"])
        for other in (flat(2, rent=1500, lat=51.502, images=["p.jpg"]), flat(3, rent=1500, beds=2, images=["p.jpg"]),
                      flat(4, rent=1610, images=["p.jpg"])):
            self.assertIsNone(signals.same_flat_two_prices(a, [a, other]))


class SelectionTests(unittest.TestCase):
    def test_at_most_two_strongest_first(self):
        l = flat(1, rent=1500, floorplan=None, images=["a"], description="short", first_listed="2026-09-01")
        rows = {l.key: [{"at": "2026-10-28", "state": "let_agreed"}]}
        out = signals.compute(l, [l], rows.get, TODAY)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0].name, "back_on_market")

    def test_sale_listing_gets_nothing(self):
        l = flat()
        l.tenure = "sale"
        self.assertEqual(signals.compute(l, [l], None, TODAY), [])

    def test_annotate_off_by_default_and_changes_only_signals(self):
        l = flat(floorplan=None, images=["a"])
        before = {k: v for k, v in vars(l).items() if k != "signals"}
        os.environ.pop("SIGNALS", None)
        signals.annotate([l], today=TODAY)
        self.assertEqual(l.signals, [])
        with patch.dict(os.environ, {"SIGNALS": "1"}):
            signals.annotate([l], today=TODAY)
        self.assertTrue(l.signals)
        self.assertEqual({k: v for k, v in vars(l).items() if k != "signals"}, before)

    def test_card_line(self):
        l = flat()
        self.assertEqual(signals.card_line(l), "")
        l.signals = [{"name": "reduced_stale", "label": "Long on the market", "evidence": "x", "strength": 2, "idea": "i"},
                     {"name": "under_presented", "label": "Thin advert, fewer people may look", "evidence": "y",
                      "strength": 2}]
        self.assertEqual(signals.card_line(l), "🔎 Long on the market · 🔎 Thin advert, fewer people may look")
        self.assertEqual(signals.card_ideas(l), ["i"])


class CardIntegrationTests(unittest.TestCase):
    def test_card_shows_labels_only_when_present(self):
        from pisobot import telegram
        l = flat()
        l.lat = l.lng = None
        plain = telegram.format_listing(l)
        self.assertNotIn("🔎", plain)
        l.signals = [{"name": "reduced_stale", "label": "Long on the market", "evidence": "x", "strength": 2}]
        self.assertIn("🔎 Long on the market", telegram.format_listing(l))


class LogTests(unittest.TestCase):
    def setUp(self):
        self.db = os.path.join(tempfile.mkdtemp(), "s.db")

    def shown(self, key_id, name="reduced_stale"):
        l = flat(key_id)
        l.signals = [{"name": name, "label": "x", "evidence": "e", "strength": 2}]
        signals.log_shown(l, path=self.db)

    def test_shown_then_saved_and_hidden(self):
        for i in (1, 2, 3):
            self.shown(i)
        self.shown(4, "under_presented")
        signals.log_action("rm:1", "s", path=self.db)
        signals.log_action("rm:2", "h", path=self.db)
        signals.log_action("rm:4", "d", path=self.db)
        r = signals.report(self.db)
        self.assertEqual(r["reduced_stale"], {"shown": 3, "saved": 1, "hidden": 1, "no_action": 1, "save_rate": 0.333})
        self.assertEqual(r["under_presented"]["hidden"], 1)

    def test_action_applies_once(self):
        self.shown(1)
        signals.log_action("rm:1", "s", path=self.db)
        signals.log_action("rm:1", "h", path=self.db)
        self.assertEqual(signals.report(self.db)["reduced_stale"]["saved"], 1)

    def test_no_signals_no_rows_and_off_without_path_or_flag(self):
        signals.log_shown(flat(), path=self.db)
        signals.log_action("rm:1", "s")                  # no path, SIGNALS unset: no-op
        self.assertEqual(signals.report(self.db), {})

    def test_logging_failure_is_swallowed(self):
        l = flat()
        l.signals = [{"name": "reduced_stale", "label": "x", "evidence": "e", "strength": 2}]
        signals.log_shown(l, path="/nonexistent-dir-xyz/\0bad")


if __name__ == "__main__":
    unittest.main()
