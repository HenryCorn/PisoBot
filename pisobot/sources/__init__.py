import re
import datetime as dt
from html import unescape


def money(s):
    """'£1,403' / '&pound; 97,500' -> 1403.0"""
    if s is None:
        return None
    m = re.search(r"\d[\d,]*(?:\.\d+)?", unescape(str(s)))
    return float(m.group(0).replace(",", "")) if m else None


def text(html):
    html = re.sub(r"<(script|style|svg)[^>]*>.*?</\1>", " ", html, flags=re.S)
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", html))).strip()


def lease_remaining(value):
    m = re.search(r"(\d{2,4})(?:\s*(?:years?|yrs?))?(?:\s+(?:lease\s+)?(?:from|commencing|starting)\b[^.;]{0,45}?\b((?:19|20)\d{2}))?",
                  str(value or ""), re.I)
    if not m:
        return None
    return max(0, int(m[1]) - (dt.date.today().year - int(m[2]) if m[2] else 0))


def detail_fields(blob):
    """Only explicit advert facts; absence stays unknown rather than becoming zero."""
    blob = text(blob)
    fields = {}
    for name, label in (("rent", r"rent"), ("service_charge", r"service\s+charges?")):
        label = rf"(?:(?:monthly|annual|yearly|weekly)\s+)?{label}"
        amount = r"£\s*([\d,]+(?:\.\d+)?)"
        unit = r"(?:per\s+(?:calendar\s+)?(?:month|annum|year|week)|p\.?c\.?m\.?|p\.?[amw]\.?|monthly|annually|yearly|weekly|/(?:month|year|week))"
        period = rf"(?:\s*\(?\s*{unit}\s*\)?)?"
        match = re.search(rf"\b{label}{period}\s*[:\-]?\s*{amount}{period}", blob, re.I)
        if not match:
            match = re.search(rf"{amount}{period}\s+{label}\b", blob, re.I)
        if match:
            value, phrase = money(match[1]), match[0].lower()
            if re.search(r"annum|annual|year|(?<![a-z])p\.?a\.?(?![a-z])", phrase):
                value /= 12
            elif re.search(r"week|(?<![a-z])p\.?w\.?(?![a-z])", phrase):
                value *= 52 / 12
            fields[name] = value
    share = re.search(r"\b(\d{1,3}(?:\.\d+)?)\s*%\s*(?:share|ownership|stake)", blob, re.I)
    full = re.search(r"\b(?:full(?:\s+market)?\s+value(?:\s+of\s+property)?|FMV|100%)\s*[:\-]?\s*£\s*([\d,]+(?:\.\d+)?)",
                     blob, re.I)
    if share and 0 < float(share[1]) <= 100:
        fields["share_pct"] = float(share[1])
    if full:
        fields["full_value"] = money(full[1])
    start = r"(?:from|commencing|starting)\b[^.;]{0,45}?\b(?:19|20)\d{2}"
    lease = re.search(rf"\blease(?:hold)?(?:\s+(?:length|remaining))?\s*[:\-]?\s*"
                      rf"(\d{{2,4}}(?:\s*years?)?(?:\s+lease)?(?:\s+{start})?)"
                      rf"|(\d{{2,4}}\s*years?\s+(?:remaining|unexpired|lease)(?:\s+{start})?)", blob, re.I)
    if lease:
        fields["lease_years"] = lease_remaining(lease[1] or lease[2])
    area = re.search(r"\b(?:total(?:\s+(?:gross|internal|floor))*(?:\s+area)?|(?:gross|internal|floor)\s+area|size)"
                     r"\s*[:\-=]?\s*(?:approx(?:\.|imately)?\s*)?([\d,.]+)\s*"
                     r"(m²|m2|sq\.?\s*m(?:etres?)?|square\s+metres?|sq\.?\s*ft|ft²|square\s+feet)\b",
                     blob, re.I)
    if area:
        value = money(area[1])
        if re.search(r"ft|feet", area[2], re.I):
            value *= 0.09290304
        if 0 < value < 10000:
            fields["floor_area_m2"] = round(value, 2)
    return fields
