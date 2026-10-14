from . import config


def score(l):
    """Lower is better. Weighted mix of cost, commute and crime, each relative to its cap."""
    w = config.RANK_WEIGHTS
    s = (w["cost"] * l.monthly_cost / config.MAX_MONTHLY_COST
         + w["travel"] * l.travel_minutes / config.MAX_TRAVEL_MINUTES
         + w["crime"] * l.crime_score / config.MAX_CRIME_SCORE)
    if l.tenure == "sale" and (l.deposit or 0) > config.PREFERRED_DEPOSIT:
        s += config.RANK_DEPOSIT_PENALTY
    if l.estimated:
        s += config.RANK_ESTIMATE_PENALTY
    s -= config.RANK_EXTRA_BED_BONUS * max(0, l.bedrooms - config.MIN_BEDROOMS)
    return s


def _same_development(a, b):
    return abs(a.lat - b.lat) < 0.0015 and abs(a.lng - b.lng) < 0.0025   # ~150 m


def best(listings, n=None, per_development=None):
    """Top n by score, with at most per_development picks from one building/site."""
    n = n or config.REPORT_TOP_N
    per_development = per_development or config.REPORT_MAX_PER_DEVELOPMENT
    top = []
    for l in sorted(listings, key=score):
        if sum(_same_development(l, t) for t in top) >= per_development:
            continue
        top.append(l)
        if len(top) == n:
            break
    return top
