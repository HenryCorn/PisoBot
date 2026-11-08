"""Preference storage: Cloudflare D1 by default, SQLite when STORAGE=local.

The Telegram Worker (worker/) records cloud button taps and commands instantly;
runs read those choices and publish the listings cards refer to.
"""
import os

from . import localdb, net

URL = "https://api.cloudflare.com/client/v4/accounts/{account}/d1/database/{database}/query"
NAMES = ("CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_D1_DATABASE_ID", "CLOUDFLARE_API_TOKEN")


def configured():
    if localdb.storage() == "local":
        return True
    return all(os.environ.get(name, "").strip() for name in NAMES)


def query(sql, *params):
    if localdb.storage() == "local":
        return localdb.query(sql, *params)
    if not configured():
        raise RuntimeError(f"{', '.join(NAMES)} must be set (add them as GitHub Actions secrets)")
    account, database, token = (os.environ[name].strip() for name in NAMES)
    r = net.session.post(URL.format(account=account, database=database),
                         json={"sql": sql, "params": list(params)},
                         headers={"Authorization": f"Bearer {token}"}, timeout=30, verify=net.VERIFY)
    try:
        res = r.json()
    except ValueError:
        res = {}
    if not res.get("success"):
        raise RuntimeError(f"D1 query failed ({r.status_code}): {res.get('errors') or r.text[:200]}")
    return res["result"][0]["results"]
