import os
import time

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


def page(url, params=None, retries=3):
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
