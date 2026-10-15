from . import config


def score(l):
    """Lower is better. Weighted mix of cost, commute friction, and crime, adjusted by taste."""
    w = config.RANK_WEIGHTS
    commute = l.commute_friction if getattr(l, "commute_friction", None) is not None else (l.travel_minutes or 0)
    s = (w["cost"] * l.monthly_cost / config.MAX_MONTHLY_COST
         + w["travel"] * commute / config.MAX_TRAVEL_MINUTES
         + w["crime"] * (l.crime_score or 0) / config.MAX_CRIME_SCORE)
    if l.tenure == "sale" and (l.deposit or 0) > config.PREFERRED_DEPOSIT:
        s += config.RANK_DEPOSIT_PENALTY
    if l.estimated:
        s += config.RANK_ESTIMATE_PENALTY
    s -= config.RANK_EXTRA_BED_BONUS * max(0, l.bedrooms - config.MIN_BEDROOMS)

    # Taste preference adjustment (-0.20 to +0.20)
    match = getattr(l, "match_score", None)
    if match is not None:
        s -= 0.20 * (match - 50) / 50

    # Red flag penalty & deal boost
    if getattr(l, "red_flags", None):
        s += 0.15 * len(l.red_flags)
    if getattr(l, "deal_tag", None):
        s -= 0.05
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


def group_by_development(listings):
    """Cluster listings within ~150 m of each other; groups ordered by their cheapest home."""
    groups = []
    for l in sorted(listings, key=lambda l: l.monthly_cost):
        for g in groups:
            if _same_development(l, g[0]):
                g.append(l)
                break
        else:
            groups.append([l])
    return groups
