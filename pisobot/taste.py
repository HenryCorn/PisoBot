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
    def __init__(self, saved, hidden, hidden_dev, seen=None, feedback=None):
        seen = seen or {}
        feedback = feedback or []
        self.feedback = feedback
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

        # Explicit user feedback parsing
        self.feedback_liked_outcodes = set()
        self.feedback_liked_coords = []
        self.feedback_disliked_outcodes = set()
        self.feedback_disliked_coords = []
        self.feedback_price_ceilings = []
        self.feedback_commute_disliked = False
        self.feedback_trap_disliked = False
        self.feedback_value_liked = False
        self.feedback_vibe_liked = False
        self.feedback_commute_liked = False

        for fb in feedback:
            key, action, reason = fb.get("key"), fb.get("action"), fb.get("reason")
            rec = saved.get(key) or seen.get(key) or {}
            outcode = _extract_outcode(rec.get("address"))
            lat, lng = rec.get("lat"), rec.get("lng")
            cost = rec.get("cost")

            if action == "s":
                if reason == "area":
                    if outcode:
                        self.feedback_liked_outcodes.add(outcode)
                    if lat and lng:
                        self.feedback_liked_coords.append((lat, lng))
                elif reason == "value":
                    self.feedback_value_liked = True
                elif reason == "vibe":
                    self.feedback_vibe_liked = True
                elif reason == "commute":
                    self.feedback_commute_liked = True
            elif action == "h":
                if reason == "area":
                    if outcode:
                        self.feedback_disliked_outcodes.add(outcode)
                    if lat and lng:
                        self.feedback_disliked_coords.append((lat, lng))
                elif reason == "price":
                    if cost:
                        self.feedback_price_ceilings.append(cost)
                elif reason == "commute":
                    self.feedback_commute_disliked = True
                elif reason == "trap":
                    self.feedback_trap_disliked = True

    @property
    def has_training_data(self):
        return bool(self.liked_coords or self.disliked_coords or self.feedback)

    def match(self, listing):
        """Calculates a 0-100% taste match score and explanatory reasons."""
        if not self.liked_coords and not self.disliked_coords and not self.feedback:
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

        # Explicit Liked Area Feedback
        if listing.outcode and listing.outcode in self.feedback_liked_outcodes:
            score += 15
            reasons.append(f"feedback favorite area ({listing.outcode})")
        elif self.feedback_liked_coords and listing.lat and listing.lng:
            if any(_km(listing.lat, listing.lng, lat, lng) <= 1.2 for lat, lng in self.feedback_liked_coords):
                score += 12
                reasons.append("near favorited feedback area")

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

        # Explicit Disliked Area Feedback
        if listing.outcode and listing.outcode in self.feedback_disliked_outcodes:
            score -= 28
            reasons.append(f"disliked area ({listing.outcode})")
        elif self.feedback_disliked_coords and listing.lat and listing.lng:
            if any(_km(listing.lat, listing.lng, lat, lng) <= 1.2 for lat, lng in self.feedback_disliked_coords):
                score -= 22
                reasons.append("near disliked area")

        # 2. Budget / Cost Sweet Spot & Price Feedback
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

        # Explicit Price Ceiling Feedback (user hid listing due to price)
        if self.feedback_price_ceilings and listing.monthly_cost:
            min_ceiling = min(self.feedback_price_ceilings)
            if listing.monthly_cost >= min_ceiling:
                score -= 20
                reasons.append(f"above feedback budget (£{min_ceiling:,.0f})")

        # Explicit Value / Savings Feedback (user saved listing for value)
        if self.feedback_value_liked:
            rent_savings = getattr(listing, "rent_savings", None)
            if rent_savings and rent_savings >= 300:
                score += 12
                reasons.append(f"high rent savings (£{rent_savings}/mo)")

        # 3. Commute Quality & Friction
        changes = getattr(listing, "changes", 0) or 0
        if changes == 0 and listing.travel_minutes and listing.travel_minutes <= 35:
            score += 10
            reasons.append("direct 0-change commute")
        elif changes >= 2:
            score -= 8

        # Explicit Commute Feedback
        if self.feedback_commute_disliked:
            if changes >= 2 or (listing.travel_minutes and listing.travel_minutes > 35):
                score -= 18
                reasons.append("high commute friction")
        if self.feedback_commute_liked:
            if changes == 0 and listing.travel_minutes and listing.travel_minutes <= 32:
                score += 12
                reasons.append("fast direct commute")

        # 4. Feature and Vibe Boosts
        tags = getattr(listing, "feature_tags", []) or []
        vibes = getattr(listing, "vibes", []) or []
        if any("Balcony" in t or "Terrace" in t for t in tags):
            score += 5
            reasons.append("outdoor space")
        if any("Bright" in v or "Modern" in v for v in vibes):
            score += 5

        # Explicit Vibe Feedback
        if self.feedback_vibe_liked:
            if vibes or any("Balcony" in t or "Terrace" in t for t in tags):
                score += 10
                reasons.append("matches preferred vibe")

        # 5. Red Flag Penalty
        red_flags = getattr(listing, "red_flags", []) or []
        if red_flags:
            score -= len(red_flags) * 15

        # Explicit Trap / Red Flag Feedback
        if self.feedback_trap_disliked:
            sc = getattr(listing, "service_charge", 0) or 0
            if red_flags or sc > 250:
                score -= 25
                reasons.append("caution: red flag alert")

        final_score = max(5, min(98, int(round(score))))
        return final_score, reasons[:3]
