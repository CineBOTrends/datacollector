"""Low-volume check for Webshare egress to District.

    py test_webshare_proxy.py

Uses one current-day District request plus an egress-IP check. It never prints
the configured proxy URL or its credentials.
"""
import json
import os
import sys
from datetime import datetime
from urllib.parse import urlsplit

import requests

from scraper.config import IST
from scraper.district_common import (
    _build_url,
    _parse_direct_page,
    _slugify,
    _venue_city,
)
from scraper.fetcher_district_sync import _do_fetch as district_fetch
from scraper.proxy_config import get_webshare_proxy, proxy_dict
from scraper.district_stealth import get_district_identity


def _read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def main():
    proxy = get_webshare_proxy()
    if not proxy:
        print("WEBSHARE_PROXY is not configured (set it in .env or the environment).")
        return 1

    parsed = urlsplit(proxy)
    print(f"Proxy configured (host={parsed.hostname}, port={parsed.port}; credentials hidden)")
    proxies = proxy_dict(proxy)
    results = []

    try:
        response = requests.get("https://ipv4.webshare.io/", proxies=proxies, timeout=20)
        response.raise_for_status()
        print(f"Proxy egress check: OK (exit IP {response.text.strip()})")
    except requests.RequestException as e:
        print(f"Proxy egress check: FAILED ({type(e).__name__})")
        return 1

    try:
        district_venues = _read_json(os.path.join("venues", "venues9.json"))
        venue = district_venues[0]
        url = _build_url(venue, datetime.now(IST).strftime("%Y-%m-%d"))
        html = district_fetch(get_district_identity(shard_id=9), url)
        data = _parse_direct_page(
            html,
            url,
            venue.get("id") or venue.get("cinema_id") or "",
            _slugify(_venue_city(venue)),
        )
        if data.get("error"):
            raise RuntimeError(str(data["error"]))
        sessions = len(data.get("pageData", {}).get("sessions") or [])
        print(f"District: OK (page parsed; {sessions} session(s) for sample venue)")
        results.append(True)
    except Exception as e:
        print(f"District: FAILED ({type(e).__name__}: "
              f"{_safe_message(e, proxy)})")
        results.append(False)

    return 0 if all(results) else 1


def _safe_message(error, proxy):
    from scraper.proxy_config import redact_proxy_credentials
    return redact_proxy_credentials(error).replace(proxy, "<proxy URL redacted>")


if __name__ == "__main__":
    sys.exit(main())
