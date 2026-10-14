import re
from html import unescape


def money(s):
    """'£1,403' / '&pound; 97,500' -> 1403.0"""
    if s is None:
        return None
    m = re.search(r"[\d,]+(?:\.\d+)?", unescape(str(s)))
    return float(m.group(0).replace(",", "")) if m else None


def text(html):
    html = re.sub(r"<(script|style|svg)[^>]*>.*?</\1>", " ", html, flags=re.S)
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", html))).strip()
