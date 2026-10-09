"""Check that the configured DISTRICT_PROXIES can reach District.

    py test_proxy.py            # 3 venues, tomorrow
    py test_proxy.py 6 2026-10-16
"""
import json
import os
import sys
from datetime import datetime, timedelta

from scraper.district_common import _build_url, _parse_direct_page, _slugify, _venue_city
from scraper.district_stealth import DistrictIdentity
from scraper.fetcher_district_sync import _do_fetch


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    date = sys.argv[2] if len(sys.argv) > 2 else (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    with open(os.path.join("venues", "districtvenues.json"), encoding="utf-8") as f:
        venues = json.load(f)[:n]

    ident = DistrictIdentity()
    if not ident.proxies:
        print("No DISTRICT_PROXIES configured (env or .env) - testing direct, expect 403 if blocked")
    else:
        print(f"{len(ident.proxies)} prox(y/ies) configured")

    ok = 0
    for v in venues:
        label = f"{v.get('id') or v.get('cinema_id')}|{v.get('city')}"
        try:
            url = _build_url(v, date)
            html = _do_fetch(ident, url)
            data = _parse_direct_page(html, url, v.get("id") or v.get("cinema_id") or "",
                                      _slugify(_venue_city(v)))
            ok += 1
            print(f"  OK      {label}: {len(data['pageData']['sessions'])} session(s)")
        except Exception as e:
            print(f"  FAILED  {label}: {e}")
    print(f"\n{ok}/{len(venues)} reachable")


if __name__ == "__main__":
    main()
