"""Quick check that the Cloudflare worker can reach District.

    py test_worker.py            # 5 venues from venues\districtvenues.json, tomorrow
    py test_worker.py 10 2026-10-16
"""
import json
import os
import sys
from datetime import datetime, timedelta

from scraper.fetcher_district_sync import fetch_via_worker, worker_enabled, _worker_settings


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    date = sys.argv[2] if len(sys.argv) > 2 else (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    if not worker_enabled():
        print("DISTRICT_WORKER_URL / DISTRICT_UA / DISTRICT_KEY missing (env or .env)")
        sys.exit(1)
    print(f"worker: {_worker_settings()[0]}  date: {date}")

    with open(os.path.join("venues", "districtvenues.json"), encoding="utf-8") as f:
        venues = json.load(f)[:n]

    ok = 0
    for v in venues:
        label = f"{v.get('id') or v.get('cinema_id')}|{v.get('city')}"
        try:
            d = fetch_via_worker(v, date)
            sessions = len(d.get("pageData", {}).get("sessions") or [])
            movies = len(d.get("meta", {}).get("movies") or [])
            ok += 1
            print(f"  OK      {label}: {movies} movie(s), {sessions} session(s)")
        except Exception as e:
            print(f"  FAILED  {label}: {e}")
    print(f"\n{ok}/{len(venues)} reachable via worker")


if __name__ == "__main__":
    main()
