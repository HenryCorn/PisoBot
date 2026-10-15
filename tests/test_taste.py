import unittest

from pisobot import ranking
from pisobot.models import Listing
from pisobot.taste import TasteProfile


def sample_home(i, **kwargs):
    fields = dict(source="stb", id=str(i), url=f"https://example.com/{i}",
                  address="Custom House, London, E16 2LS", bedrooms=1, scheme="SO",
                  monthly_cost=1050, deposit=10000, travel_minutes=30, crime_score=40,
                  lat=51.508, lng=0.025)
    fields.update(kwargs)
    return Listing(**fields)


class TasteTests(unittest.TestCase):
    def test_cold_start_is_neutral(self):
        engine = TasteProfile(saved={}, hidden=[], hidden_dev=[])
        score, reasons = engine.match(sample_home(1))
        self.assertEqual(score, 50)
        self.assertEqual(reasons, [])

    def test_saved_preferences_boost_similar_listings(self):
        saved = {
            "stb:1": {"lat": 51.508, "lng": 0.025, "address": "Custom House, E16", "cost": 1050},
            "stb:2": {"lat": 51.510, "lng": 0.027, "address": "Canning Town, E16", "cost": 1080},
        }
        engine = TasteProfile(saved=saved, hidden=[], hidden_dev=[])

        # Listing very close to saved favorites with similar budget & 0 changes
        match_candidate = sample_home(3, lat=51.509, lng=0.026, address="Victoria Dock, E16",
                                      monthly_cost=1060, changes=0, travel_minutes=28)
        score, reasons = engine.match(match_candidate)
        self.assertGreaterEqual(score, 80)
        self.assertTrue(any("near saved" in r for r in reasons))
        self.assertTrue(any("budget" in r for r in reasons))

    def test_hidden_preferences_penalize_disliked_areas(self):
        saved = {"stb:1": {"lat": 51.508, "lng": 0.025, "address": "E16", "cost": 1050}}
        hidden_dev = [[51.555, -0.280]]  # Wembley area
        seen = {"stb:99": {"lat": 51.555, "lng": -0.280, "address": "Wembley, HA9", "cost": 1450}}
        engine = TasteProfile(saved=saved, hidden=["stb:99"], hidden_dev=hidden_dev, seen=seen)

        # Listing right near the hidden development in HA9
        disliked_candidate = sample_home(4, lat=51.556, lng=-0.281, address="Wembley Park, HA9",
                                         monthly_cost=1480, changes=2)
        score, reasons = engine.match(disliked_candidate)
        self.assertLess(score, 40)
        self.assertTrue(any("hidden" in r for r in reasons))

    def test_ranking_score_prefers_high_taste_match(self):
        # Two homes with identical baseline financials and travel time
        liked_home = sample_home(1, match_score=88)
        neutral_home = sample_home(2, match_score=50)
        disliked_home = sample_home(3, match_score=25)

        s_liked = ranking.score(liked_home)
        s_neutral = ranking.score(neutral_home)
        s_disliked = ranking.score(disliked_home)

        # Lower score is better
        self.assertLess(s_liked, s_neutral)
        self.assertLess(s_neutral, s_disliked)

    def test_commute_friction_ranks_direct_route_over_multi_change(self):
        # 32m direct route vs 28m route with 2 changes and 12m walk
        direct = sample_home(1, travel_minutes=32, changes=0, walk_minutes=6)
        multi_change = sample_home(2, travel_minutes=28, changes=2, walk_minutes=12)

        self.assertLess(direct.commute_friction, multi_change.commute_friction)
        self.assertLess(ranking.score(direct), ranking.score(multi_change))


if __name__ == "__main__":
    unittest.main()
