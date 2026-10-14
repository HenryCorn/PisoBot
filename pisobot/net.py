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
