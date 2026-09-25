"""One-cinema direct District page test for shard 9."""
import os
import re
import json
from scraper.parser import parse_district_advance
from scraper.district_common import _build_url, _parse_direct_page
from scraper.district_stealth import get_district_identity


def _load_local_env():
    """Read repository .env for this local diagnostic, without logging secrets."""
    values = {}
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if not os.path.isfile(env_path):
        return values

    with open(env_path, encoding="utf-8") as env_file:
        for line in env_file:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip("\"'")
    return values


_LOCAL_ENV = _load_local_env()


def _setting(name):
    # For this local probe, an explicitly configured repository .env is the
    # source of truth; CI runs without the repository .env and uses os.environ.
    return _LOCAL_ENV.get(name) or os.environ.get(name, "")


DATE = os.environ.get("TEST_DATE", "2026-07-02")  # advance date, YYYY-MM-DD


def slug(t):
    if not t:
        return "cinema"
    t = t.lower()
    t = re.sub(r"[^a-z0-9]+", "-", t)
    return re.sub(r"-{2,}", "-", t).strip("-") or "cinema"


def main():
    venues = json.load(open("venues/districtvenues.json", encoding="utf-8"))
    if isinstance(venues, dict):
        venues = [{**v, "id": v.get("id", k)} for k, v in venues.items()]

    v = venues[0]
    url = _build_url(v, DATE)
    print("District URL:", url)
    print("-" * 60)

    ident = get_district_identity()
    r = ident.scraper.get(url, headers=ident.headers(), timeout=30, proxies=ident.proxy_dict())
    print("HTTP status:", r.status_code)
    data = _parse_direct_page(
        r.text,
        url,
        v.get("id") or v.get("cinema_id"),
        slug(v.get("city") or v.get("City")),
    )
    if data.get("error"):
        print("DISTRICT ERROR:", data.get("error"))
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
