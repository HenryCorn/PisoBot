import os
import logging
import math
import random
import re
import threading
import time
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit

import requests
import urllib3

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/141.0 Safari/537.36")

# Some corporate networks intercept TLS; set INSECURE_SSL=1 for local runs only.
VERIFY = os.environ.get("INSECURE_SSL") != "1"
if not VERIFY:
    urllib3.disable_warnings()

session = requests.Session()
session.headers.update({"User-Agent": UA, "Accept-Language": "en-GB,en;q=0.9"})
log = logging.getLogger(__name__)


def get(url, params=None, retries=3, **kw):
    for attempt in range(retries):
        try:
            r = session.get(url, params=params, timeout=30, verify=VERIFY, **kw)
            if r.status_code in (429, 500, 502, 503, 504):
                raise requests.HTTPError(f"{r.status_code} for {url}")
            r.raise_for_status()
            return r
        except requests.RequestException:
            if attempt == retries - 1:
                raise
            time.sleep(2 * (attempt + 1))


def post(url, **kw):
    r = session.post(url, timeout=30, verify=VERIFY, **kw)
    r.raise_for_status()
    return r


DECODO_URL = "https://data.decodo.com/v1/scrape"


def _legacy_page(url, params=None, retries=3):
    """HTML of a portal that blocks cloud runners: fetched by Decodo's web data API from UK
    residential IPs when DECODO_AUTH_TOKEN is set, directly otherwise (local runs, tests).
    Each call is one Decodo request (the free plan has 2,000), so callers cache results."""
    token = os.environ.get("DECODO_AUTH_TOKEN")
    if not token:
        return get(url, params=params, retries=retries).text
    body = {"target": "universal", "url": requests.Request("GET", url, params=params).prepare().url,
            "geo": "United Kingdom"}
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    for attempt in range(retries):
        try:
            r = session.post(DECODO_URL, json=body, headers=headers, timeout=180, verify=VERIFY)
            if r.status_code in (401, 402, 403):
                raise requests.HTTPError(f"Decodo rejected DECODO_AUTH_TOKEN or the plan is inactive "
                                         f"({r.status_code})", response=r)
            r.raise_for_status()
            result = (r.json().get("results") or [{}])[0]
            status = result.get("status_code") or 200
            if status >= 400 or not result.get("content"):
                raise requests.HTTPError(f"{status} for {url} via Decodo")
            return result["content"]
        except requests.RequestException as ex:
            if attempt == retries - 1 or "rejected DECODO_AUTH_TOKEN" in str(ex):
                raise
            time.sleep(5 * (attempt + 1))


class FetchBlocked(requests.HTTPError):
    """A portal block, including a host still in its cooldown period."""


class _Host:
    def __init__(self):
        self.lock = threading.Lock()
        self.next_request = 0
        self.cooldown = 0


_hosts = {}
_hosts_lock = threading.Lock()


def _interstitial(html):
    if not html.strip():
        return True
    title = re.search(r"<title\b[^>]*>(.*?)</title>", html, re.I | re.S)
    if title and re.search(r"^\s*(?:just a moment[.!…]*|attention required!?\s*\|?\s*cloudflare|"
                           r"checking your browser[.!…]*|access denied|robot check)\s*$",
                           title[1], re.I):
        return True
    # Background /cdn-cgi/challenge-platform/scripts/jsd/main.js is also present
    # on valid Foxtons pages; only an actual challenge form is a block signal.
    return bool(re.search(r'<form\b[^>]*\bid=["\']challenge-form["\']', html, re.I))


def _retry_after(value):
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        try:
            seconds = parsedate_to_datetime(value).timestamp() - time.time()
        except (TypeError, ValueError, OverflowError):
            seconds = 300
    if not math.isfinite(seconds):
        seconds = 300
    return max(1, min(seconds, 3600))


def _plain_request(url, gap):
    host = urlsplit(url).hostname
    if urlsplit(url).scheme not in ("http", "https") or not host:
        raise ValueError("FETCH_CHAIN requires an absolute HTTP(S) URL")
    with _hosts_lock:
        state = _hosts.setdefault(host.lower(), _Host())
    with state.lock:
        now = time.monotonic()
        if state.cooldown > now:
            raise FetchBlocked(f"{host} cooling down; try again in {math.ceil(state.cooldown - now)}s")
        wait = state.next_request - now
        if wait > 0:
            time.sleep(wait)
        try:
            response = session.get(url, timeout=30, verify=VERIFY, allow_redirects=False)
            log.info("portal_fetch host=%s tier=plain status=%s", host, response.status_code)
            if response.status_code in (403, 429, 503) or (
                    response.status_code < 300 and _interstitial(response.text)):
                state.cooldown = time.monotonic() + _retry_after(response.headers.get("Retry-After"))
                log.warning("portal_block host=%s status=%s", host, response.status_code)
                raise FetchBlocked(f"Blocked response ({response.status_code}) from {host}", response=response)
            response.raise_for_status()
            return response
        finally:
            state.next_request = time.monotonic() + random.uniform(gap * 0.5, gap * 1.5)


def _plain_page(url, params, gap):
    url = requests.Request("GET", url, params=params).prepare().url
    for _ in range(6):
        response = _plain_request(url, gap)
        if response.status_code not in (301, 302, 303, 307, 308):
            return response.text
        if not response.headers.get("Location"):
            raise requests.HTTPError("Redirect without Location", response=response)
        url = urljoin(url, response.headers["Location"])
    raise requests.TooManyRedirects("Too many redirects fetching page", response=response)


def page(url, params=None, retries=3):
    """Unset FETCH_CHAIN preserves the original cloud/Decodo behavior exactly.

    Explicit plain[,decodo] makes one attempt per tier, serializes each host and
    waits FETCH_MIN_GAP_S (default 4, jitter ±50%) after a request. Blocks put the
    host in a process-local cooldown (Retry-After, capped at one hour; default
    five minutes); later calls fail fast. Only real blocks may use the next tier.
    Redirects also respect host pacing. No browser/challenge bypass is attempted.
    """
    chain = os.environ.get("FETCH_CHAIN")
    if chain is None:
        return _legacy_page(url, params=params, retries=retries)
    tiers = [tier.strip() for tier in chain.split(",")]
    if tiers not in (["plain"], ["decodo"], ["plain", "decodo"]):
        raise ValueError("FETCH_CHAIN must be plain, decodo, or plain,decodo")
    if "decodo" in tiers and not os.environ.get("DECODO_AUTH_TOKEN"):
        raise ValueError("FETCH_CHAIN includes decodo but DECODO_AUTH_TOKEN is empty")
    try:
        gap = float(os.environ.get("FETCH_MIN_GAP_S", "4"))
    except ValueError as ex:
        raise ValueError("FETCH_MIN_GAP_S must be a finite number between 0 and 3600") from ex
    if not math.isfinite(gap) or not 0 <= gap <= 3600:
        raise ValueError("FETCH_MIN_GAP_S must be a finite number between 0 and 3600")
    for index, tier in enumerate(tiers):
        try:
            if tier == "plain":
                return _plain_page(url, params, gap)
            html = _legacy_page(url, params=params, retries=1)
            if _interstitial(html):
                raise FetchBlocked("Decodo returned an empty page or challenge interstitial")
            return html
        except FetchBlocked:
            if index == len(tiers) - 1:
                raise
