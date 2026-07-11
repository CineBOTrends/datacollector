"""
One-cinema end-to-end test for District shard 9.

Run from the minttrack folder:
    python test_district.py

It picks the first cinema in venues/districtvenues.json, calls your LIVE
worker with your headers, then runs the real parser and reports exactly where
rows are kept or dropped. Override the advance date with TEST_DATE=YYYY-MM-DD.
"""
import os
import re
import json
import requests
from scraper.parser import parse_district_advance

WORKER = os.environ.get("DISTRICT_WORKER_URL", "")
UA = os.environ.get("DISTRICT_UA", "")
KEY = os.environ.get("DISTRICT_KEY", "")
DATE = os.environ.get("TEST_DATE", "2026-07-02")  # advance date, YYYY-MM-DD


def slug(t):
    if not t:
        return "cinema"
    t = t.lower()
    t = re.sub(r"[^a-z0-9]+", "-", t)
    return re.sub(r"-{2,}", "-", t).strip("-") or "cinema"


def main():
    if not KEY:
        print("WARNING: DISTRICT_KEY is empty -> worker will 401. Set it first.\n")

    venues = json.load(open("venues/districtvenues.json", encoding="utf-8"))
    if isinstance(venues, dict):
        venues = [{**v, "id": v.get("id", k)} for k, v in venues.items()]

    v = venues[0]
    params = {
        "cinema_id": v.get("id") or v.get("cinema_id"),
        "slug": v.get("slug") or slug(v.get("district_name") or v.get("name")),
        "city": slug(v.get("city") or v.get("City")),
        "date": DATE,
    }
    print("Worker URL :", WORKER)
    print("Params     :", params)
    print("Headers    : User-Agent=%s  x-api-key=%s***" % (UA, KEY[:4]))
    print("-" * 60)

    r = requests.get(WORKER, params=params, headers={"User-Agent": UA, "x-api-key": KEY}, timeout=30)
    print("HTTP status:", r.status_code)
    try:
        data = r.json()
    except Exception:
        print("Body (not JSON):", r.text[:300]); return

    if data.get("error"):
        print("WORKER ERROR:", data.get("error"))
        print("(404 = wrong CD id / no page; 401 = bad UA/key; no_next_data = page changed)")
        return

    movies = data.get("meta", {}).get("movies", []) or []
    sessions = data.get("pageData", {}).get("sessions", []) or []
    print("movies returned   :", len(movies))
    print("sessions returned :", len(sessions))
    if movies:
        print("  movie[0]  id=%r  name=%r" % (movies[0].get("id"), movies[0].get("name")))
    if sessions:
        print("  session[0] mid=%r  sid=%r  showTime=%r"
              % (sessions[0].get("mid"), sessions[0].get("sid"), sessions[0].get("showTime")))
        match = any(str(m.get("id")) == str(sessions[0].get("mid")) for m in movies)
        print("  session[0].mid matches a movie id?", match)
    print("-" * 60)

    rows = parse_district_advance([{"venue": v, "data": data}], DATE.replace("-", ""))
    print("PARSED ROWS:", len(rows))
    for row in rows[:5]:
        print("   %s | sold=%s gross=%s occ=%s" %
              (row["movie"], row["ticketsSold"], row["grossRevenue"], row["occupancy"]))

    print("-" * 60)
    if sessions and not rows:
        print("DIAGNOSIS: worker returned sessions but parser kept 0 -> session mid")
        print("does not match any movie id. Your deployed worker is an OLDER version;")
        print("redeploy the latest district_cinema_worker.js.")
    elif not sessions:
        print("DIAGNOSIS: no sessions for this cinema/date. Try TEST_DATE=<today> or a")
        print("cinema you know has shows. (Many cinemas have advance dates unopened.)")
    elif rows:
        print("DIAGNOSIS: full pipeline OK. Your earlier 0-shows run used stale code;")
        print("re-run the shard fresh:  python cli.py scrape --mode advance --shard 9 --date %s" % DATE.replace("-", ""))


if __name__ == "__main__":
    main()
