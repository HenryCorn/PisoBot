from . import config


def monthly_repayment(principal, rate_pct=None, years=None):
    rate_pct = config.MORTGAGE_RATE if rate_pct is None else rate_pct
    years = config.MORTGAGE_YEARS if years is None else years
    if principal <= 0:
        return 0.0
    r = rate_pct / 100 / 12
    n = years * 12
    return principal * r / (1 - (1 + r) ** -n)


def compute(listing):
    """Fill deposit / mortgage / monthly_cost. Returns False if the deposit can't be met."""
    if listing.tenure == "rent":
        listing.deposit = 0
        listing.mortgage = 0
        listing.monthly_cost = (listing.rent or 0) + (listing.service_charge or 0)
        return listing.rent is not None

    if not listing.share_price:
        return False

    if listing.share_pct and not listing.full_value:
        listing.full_value = listing.share_price * 100 / listing.share_pct
    if listing.full_value and not listing.share_pct:
        listing.share_pct = round(listing.share_price * 100 / listing.full_value)

    if listing.rent is None:
        if not listing.full_value:
            # Unknown share: assume 25%, but never a full value below a typical London price
            # (a £50k "share" is usually a 10-15% share of a £350k flat -> much higher rent).
            floor = config.MIN_FULL_VALUE_BY_BEDS.get(min(listing.bedrooms, 3), 0)
            listing.full_value = max(listing.share_price * 4, floor)
            listing.share_pct = round(listing.share_price * 100 / listing.full_value)
            listing.estimated.append("share %")
        unowned = listing.full_value - listing.share_price
        listing.rent = unowned * config.SO_RENT_RATE / 100 / 12
        listing.estimated.append("rent")
    if listing.service_charge is None:
        listing.service_charge = config.DEFAULT_SERVICE_CHARGE
        listing.estimated.append("service charge")

    min_dep = listing.min_deposit or listing.share_price * 0.05
    if min_dep > config.MAX_DEPOSIT:
        return False
    listing.deposit = min(max(min_dep, config.PREFERRED_DEPOSIT), listing.share_price)
    listing.mortgage = monthly_repayment(listing.share_price - listing.deposit)
    listing.monthly_cost = listing.mortgage + listing.rent + listing.service_charge
    return True
