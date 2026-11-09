"""Conservative rental exclusions: explicit restrictions reject; ambiguous mentions warn."""
import re

ROOM_LET = re.compile(
    r"\bshar(?:e|ed|ing)\s+(?:(?:a|an|the|one|spacious|large|modern|fitted|communal|"
    r"open[- ]plan|fully[- ]fitted|well[- ]equipped)\s+){0,3}"
    r"(?:kitchen|bathroom|house|flat|accommodation|living\s+room|lounge)\b"
    r"|\ben[\s-]?suite\s+room\b|\bco[\s-]?living\b"
    r"|\bbedsit\s+(?:in|to\s+(?:rent|let))\b", re.I)
AMBIGUOUS = {"studio", "reserved", "double room", "single room", "retirement", "older person",
             "over 55", "over-55", "over 55s", "over 60", "aged 60", "aged 55"}


def _negated(value, start):
    before = re.split(r"[.!?;,\n]", value[max(0, start - 60):start])[-1]
    return bool(re.search(
        r"\b(?:no\s+need\s+to|not|without|no)\s+"
        r"(?:(?:a|an|any|the|longer|currently|for)\s+){0,3}$", before, re.I))


def is_room_let(value):
    return any(not _negated(value, match.start()) for match in ROOM_LET.finditer(value))


def _explicit(value, match, keyword):
    before, after = value[:match.start()], value[match.end():]
    if keyword == "studio":
        return bool(re.match(r"\s+(?:flat|apartment|accommodation)\b", after, re.I)
                    or re.search(r"(?:^|[.!?\n])\s*(?:(?:a|an|modern|spacious|bright|"
                                 r"self-contained|furnished|unfurnished)\s+){0,3}$", before, re.I))
    if keyword == "reserved":
        return bool(re.search(r"\b(?:property|flat|apartment|home)\s+(?:is\s+)?(?:now\s+)?$", before, re.I)
                    or re.fullmatch(r"\s*reserved[.!]?\s*", value, re.I))
    if keyword in ("double room", "single room"):
        return bool(re.match(r"\s+(?:to\s+(?:rent|let)|for\s+(?:rent|let)|in\s+(?:a\s+)?shared)\b",
                             after, re.I) or re.fullmatch(r"\s*(?:double|single)\s+room[.!]?\s*", value, re.I))
    if keyword == "retirement":
        if re.search(r"\b(?:near|opposite|next\s+to)\s+(?:a\s+)?$", before, re.I):
            return False
        return bool(re.match(r"\s+(?:flat|apartment|property|housing|living|home|homes|development|scheme|accommodation)\b",
                             after, re.I))
    return bool(match.group().lower().endswith(("55s", "60s"))
                or re.match(r"\s*(?:only\b|years?\s+of\s+age\b)", after, re.I)
                or re.search(r"\b(?:for|must\s+be|applicants?|residents?|tenants?|restricted\s+to)\s*$",
                             before, re.I))


def classify(value, keywords):
    rejected, warnings = [], []
    for keyword in keywords:
        pattern = re.escape(keyword).replace(r"\ ", r"[\s-]+")
        if keyword in ("over 55", "over-55", "over 55s"):
            pattern = r"over[\s-]+55s?"
        elif keyword == "over 60":
            pattern = r"over[\s-]+60s?"
        if keyword == "students only":
            pattern = r"(?:students?[\s-]+only|only\s+(?:for\s+)?students?)"
        for match in re.finditer(r"(?<!\w)" + pattern + r"(?!\w)", value, re.I):
            if _negated(value, match.start()):
                continue
            before, after = value[:match.start()], value[match.end():]
            if keyword == "room in" and re.search(r"\b(?:living|dining|reception|sitting|family)\s+$", before, re.I):
                continue
            if keyword == "reserved" and re.match(r"\s+(?:parking|car|space|spaces)\b", after, re.I):
                continue
            if keyword in AMBIGUOUS and re.match(r"\s*(?:sq\b|sqm\b|square\b|m2\b|m²|ft\b)", after, re.I):
                continue
            if keyword in AMBIGUOUS and not _explicit(value, match, keyword):
                warnings.append(keyword)
            else:
                rejected.append(keyword)
    if is_room_let(value):
        rejected.append("shared accommodation")
    return list(dict.fromkeys(rejected)), list(dict.fromkeys(warnings))
