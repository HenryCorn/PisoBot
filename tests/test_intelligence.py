import datetime as dt
import unittest

from pisobot import intelligence
from pisobot.models import Listing


class IntelligenceTests(unittest.TestCase):
    def test_red_flag_trap_detection(self):
        self.assertIn("⚠️ Auction / Cash only",
                      intelligence.detect_red_flags("Great opportunity for cash buyers only."))
        self.assertIn("⚠️ Auction / Cash only",
                      intelligence.detect_red_flags("For sale by modern method of auction."))
        self.assertIn("⚠️ Above commercial",
                      intelligence.detect_red_flags("Situated above a takeaway on high street."))
        self.assertIn("⚠️ Needs refurbishment",
                      intelligence.detect_red_flags("In need of full modernisation throughout."))
        self.assertIn("⚠️ Needs refurbishment",
                      intelligence.detect_red_flags("Property has history of subsidence."))
        self.assertIn("⚠️ Escalating ground rent",
                      intelligence.detect_red_flags("Lease with doubling ground rent every 10 years."))
        self.assertEqual(intelligence.detect_red_flags("Stunning modern apartment with balcony."), [])

    def test_semantic_vibe_detection(self):
        vibes1 = intelligence.detect_vibes("Sun-drenched living room with floor-to-ceiling windows.")
        self.assertIn("☀️ Bright & Airy", vibes1)

        vibes2 = intelligence.detect_vibes("Located on a quiet residential cul-de-sac.")
        self.assertIn("🌿 Quiet & Leafy", vibes2)

        vibes3 = intelligence.detect_vibes("High-spec modern kitchen with integrated appliances and quartz worktops.")
        self.assertIn("✨ Modern finish", vibes3)

        vibes4 = intelligence.detect_vibes("Generous double bedrooms and spacious open plan living.")
        self.assertIn("📐 Spacious", vibes4)

        vibes5 = intelligence.detect_vibes("Stunning canal-side location overlooking the dock.")
        self.assertIn("🌊 Waterside / Views", vibes5)

    def test_opportunity_cost_savings_vs_private_rent(self):
        l1 = Listing("stb", "1", "https://example.com/1", "Custom House, London, E16 2LS", 1, "Shared Ownership",
                     monthly_cost=1050)
        # E16 is Newham (1-bed median rent = £1,600)
        # Savings = 1600 - 1050 = 550
        self.assertEqual(intelligence.opportunity_cost_savings(l1), 550)

        # 2-bed in Hackney (E8 median = £2,300)
        l2 = Listing("stb", "2", "https://example.com/2", "Dalston, London, E8 1AA", 2, "Shared Ownership",
                     monthly_cost=1400)
        self.assertEqual(intelligence.opportunity_cost_savings(l2), 900)

        # When monthly cost exceeds median rent, returns None
        l3 = Listing("stb", "3", "https://example.com/3", "Custom House, London, E16 2LS", 1, "Shared Ownership",
                     monthly_cost=1800)
        self.assertIsNone(intelligence.opportunity_cost_savings(l3))

    def test_green_space_proximity(self):
        # Coordinates ~300m from Victoria Park (51.536, -0.038)
        parks = intelligence.nearby_green_spaces(51.538, -0.038)
        self.assertTrue(any("Victoria Park" in p for p in parks))

        # Far away from major parks
        parks_far = intelligence.nearby_green_spaces(51.65, 0.20)
        self.assertEqual(parks_far, [])

    def test_deal_hunter_signals(self):
        l = Listing("stb", "1", "https://example.com/1", "Stratford, E15", 1, "Shared Ownership", monthly_cost=1000)
        now = dt.datetime(2026, 10, 15, 12, 0)

        # Sitting for 60 days
        seen_60d = {"first": "2026-08-15T06:30", "cost": 1000}
        self.assertIn("Negotiation potential", intelligence.deal_hunter_signals(l, seen_60d, now=now))

        # Recent price cut
        seen_cut = {"first": "2026-10-10T06:30", "cost": 1080}
        self.assertIn("Price cut (-£80/mo)", intelligence.deal_hunter_signals(l, seen_cut, now=now))

        # Brand new listing without drop
        seen_new = {"first": "2026-10-14T06:30", "cost": 1000}
        self.assertIsNone(intelligence.deal_hunter_signals(l, seen_new, now=now))


if __name__ == "__main__":
    unittest.main()
