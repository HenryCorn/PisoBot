"""Conservative rental exclusions: explicit restrictions reject; ambiguous mentions warn."""
import re

from . import config

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


def _noncurrent_room_prefix(before):
    # Cover nested phrases such as 'formerly a double room to rent' and
    # 'not a room in a shared house', without extending across but/new clauses.
    return bool(re.search(
        r"\b(?:not|without|no(?:\s+longer)?|formerly|previously|once)\s+"
        r"(?:(?:a|an|the|double|single|room|in|shared|house|flat|to|rent|let|"
        r"marketed|advertised|used|described|rented|as)\s+){0,10}$", before, re.I))


def explicit_room_let(value):
    """Current explicit room/share evidence that a whole-flat model answer cannot override."""
    text = value or ""
    keywords = ("room in", "room only", "room to rent", "double room", "single room",
                "flat share", "flatshare", "house share", "houseshare", "shared house",
                "shared flat", "sharing with", "shared kitchen", "shared bathroom",
                "communal bathroom", "multiple occupation")
    for keyword in keywords:
        pattern = re.escape(keyword).replace(r"\ ", r"[\s-]+")
        for match in re.finditer(r"(?<!\w)" + pattern + r"(?!\w)", text, re.I):
            before = text[:match.start()]
            if _noncurrent_room_prefix(before) or _negated(text, match.start()):
                continue
            if keyword == "room in" and re.search(r"\b(?:living|dining|reception|sitting|family)\s+$", before, re.I):
                continue
            if keyword in AMBIGUOUS and not _explicit(text, match, keyword):
                continue
            return True
    for match in ROOM_LET.finditer(text):
        if not _negated(text, match.start()) and not _noncurrent_room_prefix(text[:match.start()]):
            return True
    return False


def exclude_keywords():
    """config.EXCLUDE_KEYWORDS for private rentals, without "studio" when this person allows studios."""
    if config.STUDIO_MIN_AREA is None:
        return config.EXCLUDE_KEYWORDS
    return [k for k in config.EXCLUDE_KEYWORDS if k != "studio"]


def is_studio(value):
    """Only a current explicit studio mention, not another rejection or a historical use."""
    text = value or ""
    for match in re.finditer(r"\bstudio\b", text, re.I):
        before = text[:match.start()]
        historical = re.search(r"\b(?:formerly|previously|once)\s+(?:(?:a|an)\s+)?$", before, re.I)
        if not historical and not _negated(text, match.start()) and _explicit(text, match, "studio"):
            return True
    return False


def furnishing(value):
    """What an advert says about furniture: furnished, unfurnished, part, flexible, or None (not stated)."""
    text = str(value or "").lower()
    kinds = set()
    for m in re.finditer(r"\b(un[- ]?|part(?:ly)?[- ]?)?furnished\b", text):
        before = re.split(r"[.!?;\n]", text[:m.start()])[-1]
        after = re.split(r"[.!?;\n]", text[m.end():])[0]
        # Do not infer this home's furnishing from a nearby/show property or a
        # landlord's example. Uncertain references stay unknown, never a hard opposite.
        if re.search(r"\b(?:next to|opposite|near|neighbour(?:ing)?|neighbor(?:ing)?)\b", before) \
                or re.match(r"\s+(?:show\s+(?:flat|apartment|home)|neighbou?r)", after):
            continue
        prefix = (m[1] or "").strip(" -")
        # Not fully furnished may mean part-furnished, not an explicit unfurnished let.
        if re.search(r"\b(?:not|isn't)\s+fully\s+$", before):
            continue
        # Incidental furniture in a room/facility is not the state of the whole let.
        if re.match(r"\s+(?:communal|shared|show|display|wardrobes?|furniture|lounge|reception)\b", after):
            continue
        negated = bool(re.search(r"\b(?:not|isn't|without)\s+$", before))
        if negated:
            if not prefix:
                kinds.add("unfurnished")
            continue  # 'not unfurnished/part furnished' does not establish a positive state
        kinds.add("unfurnished" if prefix.startswith("un") else "part" if prefix else "furnished")
    if not kinds:
        return None
    if re.search(r"\blandlord is flexible\b|\bfurnished\s*(?:or|/)\s*un[- ]?furnished\b"
                 r"|\bun[- ]?furnished\s*(?:or|/)\s*furnished\b", text) or {"furnished", "unfurnished"} <= kinds:
        return "flexible"
    return "unfurnished" if "unfurnished" in kinds else "part" if "part" in kinds else "furnished"
