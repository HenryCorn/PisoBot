"""Taste profile & recommendation engine that learns from user Save (Like) and Hide (Dislike) taps."""
import math
import re
from collections import Counter


def _km(lat1, lng1, lat2, lng2):
    return math.hypot((lat1 - lat2) * 111.2, (lng1 - lng2) * 111.2 * math.cos(math.radians(lat1)))


def _extract_outcode(address):
    m = re.search(r"\b([A-Z]{1,2}\d[A-Z\d]?)\b", (address or "").upper())
    return m.group(1) if m else ""


class TasteProfile:
    def __init__(self, saved, hidden, hidden_dev, seen=None):
        seen = seen or {}
        self.saved_homes = list(saved.values())
        self.liked_coords = [(h["lat"], h["lng"]) for h in self.saved_homes if h.get("lat") and h.get("lng")]
        self.liked_outcodes = Counter(
            _extract_outcode(h.get("address")) for h in self.saved_homes if h.get("address")
        )
        self.liked_costs = [h["cost"] for h in self.saved_homes if h.get("cost")]

        self.disliked_coords = [[lat, lng] for lat, lng in hidden_dev]
        for key in hidden:
            rec = seen.get(key)
            if rec and rec.get("lat") and rec.get("lng"):
                self.disliked_coords.append((rec["lat"], rec["lng"]))
        self.disliked_outcodes = Counter(
            _extract_outcode(seen[k].get("address")) for k in hidden if k in seen and seen[k].get("address")
        )

    @property
    def has_training_data(self):
        return bool(self.liked_coords or self.disliked_coords)

    def match(self, listing):
        """Calculates a 0-100% taste match score and explanatory reasons."""
        if not self.liked_coords and not self.disliked_coords:
            return 50, []

        score = 50.0
        reasons = []

        # 1. Geographic Affinity
        if self.liked_coords and listing.lat and listing.lng:
            closest_liked_km = min(_km(listing.lat, listing.lng, lat, lng) for lat, lng in self.liked_coords)
            if closest_liked_km <= 1.0:
                score += 20
                reasons.append(f"near saved home ({closest_liked_km:.1f}km)")
            elif closest_liked_km <= 2.2:
                score += 10
                reasons.append(f"near saved area ({closest_liked_km:.1f}km)")

        if listing.outcode and self.liked_outcodes.get(listing.outcode):
            score += 10
            reasons.append(f"favorite area ({listing.outcode})")

        # Geographic Dislikes
        if self.disliked_coords and listing.lat and listing.lng:
            closest_disliked_km = min(_km(listing.lat, listing.lng, lat, lng) for lat, lng in self.disliked_coords)
            if closest_disliked_km <= 0.6:
                score -= 22
                reasons.append("near hidden building")
            elif closest_disliked_km <= 1.2:
                score -= 10

        if listing.outcode and self.disliked_outcodes.get(listing.outcode) and not self.liked_outcodes.get(listing.outcode):
            score -= 10

        # 2. Budget / Cost Sweet Spot
        if self.liked_costs and listing.monthly_cost:
            avg_cost = sum(self.liked_costs) / len(self.liked_costs)
            diff_ratio = abs(listing.monthly_cost - avg_cost) / avg_cost
            if diff_ratio <= 0.08:
                score += 12
                reasons.append("budget sweet spot")
            elif diff_ratio <= 0.18:
                score += 6
            if listing.monthly_cost > max(self.liked_costs) + 150:
                score -= 12

        # 3. Commute Quality & Friction
        changes = getattr(listing, "changes", 0) or 0
        if changes == 0 and listing.travel_minutes and listing.travel_minutes <= 35:
            score += 10
            reasons.append("direct 0-change commute")
        elif changes >= 2:
            score -= 8

        # 4. Feature and Vibe Boosts
        tags = getattr(listing, "feature_tags", []) or []
        vibes = getattr(listing, "vibes", []) or []
        if any("Balcony" in t or "Terrace" in t for t in tags):
            score += 5
            reasons.append("outdoor space")
        if any("Bright" in v or "Modern" in v for v in vibes):
            score += 5

        # 5. Red Flag Penalty
        red_flags = getattr(listing, "red_flags", []) or []
        if red_flags:
            score -= len(red_flags) * 15

        final_score = max(5, min(98, int(round(score))))
        return final_score, reasons[:3]
