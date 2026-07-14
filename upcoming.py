#!/usr/bin/env python3
"""
Opening-day advance tracking for upcoming releases.

What this is NOT: a D+3/D+5/D+7 sweep. Those offsets are arbitrary — they say
nothing about a film. What matters for a new release is its OPENING DAY: the
advance bookings piled up for the day it actually lands.

Two phases:

  1. DISCOVER - probe the next `window_days` with a SINGLE shard and read
                District's movieInfo. A film whose releaseDate equals the date
                it's playing on, and which isn't already running today, is an
                upcoming release; that date is its opening day. Cached per day —
                the probe is the expensive part, and a release date doesn't
                change hour to hour.

  2. COLLECT  - full 9-shard advance scrape on each discovered opening day, then
                drop every film that isn't opening that day. (Running films sell
                tickets days ahead too and would otherwise swamp it.)

Output lands in the normal advance tree (advance/data/<release_date>/).
"""
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

IST = timezone(timedelta(hours=5, minutes=30))

DEFAULT_WINDOW_DAYS = 21      # how far ahead bookings realistically open
CACHE = os.path.join("upcoming", "release_dates.json")

_RELEASE_KEYS = ("releaseDate", "releasedate", "release_date", "releaseDateText",
                 "release", "releaseOn", "releasedOn")


def _load_env_file(path=".env"):
    """Minimal .env reader (no python-dotenv dependency)."""
    if not os.path.exists(path):
        print(f"  ! {path} not found — DISTRICT_* must be in the environment")
        return
    loaded = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v
                loaded.append(k)
    if loaded:
        print(f"  loaded from {path}: {', '.join(loaded)}")
    missing = [k for k in ("DISTRICT_WORKER_URL", "DISTRICT_UA", "DISTRICT_KEY")
               if not os.environ.get(k)]
    if missing:
        print(f"  ! MISSING: {', '.join(missing)} -> District will return 401")


def today_ist():
    return datetime.now(IST).date()


def ymd(d):
    return d.strftime("%Y%m%d")


def _parse_release(info):
    for k in _RELEASE_KEYS:
        v = info.get(k)
        if v in (None, "", 0):
            continue
        if isinstance(v, (int, float)):
            try:
                ts = float(v)
                if ts > 1e12:
                    ts /= 1000.0
                return datetime.fromtimestamp(ts, tz=timezone.utc).date()
            except (ValueError, OverflowError, OSError):
                continue
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(v).strip())
        if m:
            try:
                return datetime(int(m[1]), int(m[2]), int(m[3])).date()
            except ValueError:
                continue
    return None


def _load(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _save(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _rows(path):
    d = _load(path)
    return d if isinstance(d, list) else []


def titles_playing_today():
    """Films in today's daily feed — already released, so never 'upcoming'."""
    dc = ymd(today_ist())
    rows = _rows(os.path.join("daily", "data", dc, "finalsummary.json")) or \
        _rows(os.path.join("daily", "data", dc, "finaldetailed.json"))
    return {r.get("movie") for r in rows if r.get("movie")}


def config():
    try:
        import yaml
        with open(os.path.join(PROJECT_ROOT, "schedule_config.yaml"), "r") as f:
            cfg = (yaml.safe_load(f) or {}).get("upcoming") or {}
    except Exception:
        cfg = {}
    return {"window_days": int(cfg.get("window_days", DEFAULT_WINDOW_DAYS))}


# ------------------------------------------------------------------ discover
# The probe must be CHEAP and must see movieInfo.
#
#  - Cheap: a "shard" is a slice of ~445 venues, not a slice of work. Probing 21
#    dates with a whole shard took ~4.5 min PER DATE (~95 min) and blew the job
#    timeout. We only need enough venues to spot a nationwide release, so we hit
#    a handful in the biggest cities instead.
#  - movieInfo: only the DISTRICT parser emits it (shards 1-8 are BMS and carry
#    none). Probing a BMS shard could never have found a release date at all.
PROBE_CITIES = ("Hyderabad", "Bengaluru", "Chennai", "Mumbai", "New Delhi")
PROBE_PER_CITY = 3          # 15 venues total — plenty to see a wide release


def probe_venues():
    """A few District venues in each major city."""
    with open(os.path.join("venues", "districtvenues.json"), encoding="utf-8") as f:
        allv = json.load(f)
    picked, seen = [], {c: 0 for c in PROBE_CITIES}
    for v in allv:
        city = v.get("city")
        if city in seen and seen[city] < PROBE_PER_CITY:
            picked.append(v)
            seen[city] += 1
        if all(n >= PROBE_PER_CITY for n in seen.values()):
            break
    return picked


def probe_date(date_code, venues, logger, with_raw=False):
    """District rows (with movieInfo) for one date, from a few venues only."""
    import asyncio
    from scraper.fetcher_async import fetch_all_async
    from scraper.parser import parse_district_advance

    dd = f"{date_code[:4]}-{date_code[4:6]}-{date_code[6:8]}"

    async def _go():
        results, _err, _failed = await fetch_all_async(venues, dd, "advance", logger)
        return results

    raw = asyncio.run(_go())
    rows = parse_district_advance(raw, date_code)
    return (rows, raw) if with_raw else rows


def dump_movie_keys(raw, limit=2):
    """Print the RAW District movie objects, keys and all.

    We keep guessing what District calls the release date and keep being wrong.
    This prints what it actually sends, so we stop guessing.
    """
    print("  --- raw District movie objects (diagnostic) ---")
    shown = 0
    for res in raw:
        movies = ((res.get("data") or {}).get("meta") or {}).get("movies") or []
        for m in movies:
            print(f"    keys: {sorted(m.keys())}")
            rel_ish = {k: v for k, v in m.items() if "releas" in k.lower()
                       or k.lower() in ("rd", "opendate", "openingdate")}
            print(f"    release-ish fields: {rel_ish or 'NONE FOUND'}")
            print(f"    name={m.get('name')!r}  isNew={m.get('isNew')!r}")
            shown += 1
            if shown >= limit:
                print("  --- end diagnostic ---")
                return
    print("  (no movie objects in the sample)")


def discover_opening_days(window_days=None, probe_shard=None, force=False,
                          verbose=False):
    """Return {release_date(YYYYMMDD): [titles opening that day]}.

    THE RULE: District's own releaseDate must be in the FUTURE. That is the only
    thing that makes a film "upcoming" — District states it plainly on the movie
    page ("Releasing 17 July 2026") and carries it in movieInfo.

    What this deliberately does NOT do:
      * infer from "first date we saw it playing" — every running film sells
        tickets days ahead, so that swept up Lenin, Dhamaal 4 and everything else.
      * depend on today's daily feed to know what's already running — that file
        lives in R2, is absent from a CI checkout, and silently excluded nothing
        (the log said "0 title(s) already running today"). A film with a past
        release date is already out; we don't need a second source to say so.
      * guess when releaseDate is missing — no date, not upcoming. Re-releases
        (Rakta Charitra, Manmadha [2005]) carry old dates and drop out here.
    """
    cfg = config()
    window_days = window_days or cfg["window_days"]

    cached = _load(CACHE)
    if cached and not force and cached.get("probed_on") == str(today_ist()):
        print(f"  using cached opening days (probed {cached['probed_on']})")
        return cached.get("opening_days", {})

    from services.logger import get_logger
    logger = get_logger(shard_id=None, log_file=None)

    today = today_ist()
    venues = probe_venues()
    print(f"  probing D+1..D+{window_days} across {len(venues)} District venues")
    print(f"  rule: District releaseDate > {today}")

    upcoming = {}          # title -> release date
    seen_titles = set()
    for off in range(1, window_days + 1):
        d = today + timedelta(days=off)
        dc = ymd(d)
        try:
            rows = probe_date(dc, venues, logger)
        except Exception as e:
            print(f"    ! probe {dc} FAILED ({type(e).__name__}: {e})")
            continue

        hits = []
        for r in rows:
            title = r.get("movie")
            if not title or title in upcoming:
                continue
            seen_titles.add(title)
            rel = _parse_release(r.get("movieInfo") or {})
            if rel and rel > today:            # not out yet -> upcoming
                upcoming[title] = rel
                hits.append(f"{title} (releases {rel})")

        if verbose:
            print(f"    {dc}: {len(rows)} rows"
                  + (f" | NEW: " + "; ".join(hits) if hits else ""))

    print(f"  {len(seen_titles)} distinct title(s) seen, "
          f"{len(upcoming)} not yet released")

    opening = {}
    for title, rel in upcoming.items():
        opening.setdefault(ymd(rel), []).append(title)
    for k in opening:
        opening[k] = sorted(opening[k])

    if opening:
        print("  opening days found:")
        for dc, titles in sorted(opening.items()):
            print(f"    {dc}: {', '.join(titles)}")
    else:
        print("    no upcoming releases with open bookings in the window")
        # Nothing found usually means District isn't sending a release date at
        # all (its showtime payload may only carry the movie card). Show what it
        # actually sends so we can stop guessing at key names.
        try:
            probe_dc = ymd(today + timedelta(days=3))
            _rows, raw = probe_date(probe_dc, venues, logger, with_raw=True)
            dump_movie_keys(raw)
        except Exception as e:
            print(f"  (diagnostic dump failed: {e})")

    _save(CACHE, {"probed_on": str(today), "opening_days": opening})
    return opening


# -------------------------------------------------------------------- filter
def filter_to_opening(date_code, titles):
    """Keep only the films actually opening on `date_code`."""
    base = os.path.join("advance", "data", date_code)
    detailed_p = os.path.join(base, "finaldetailed.json")
    summary_p = os.path.join(base, "finalsummary.json")

    detailed = _rows(detailed_p)
    if not detailed:
        print(f"  ! nothing combined for {date_code}")
        return 0

    keep = set(titles)
    kept = [r for r in detailed if r.get("movie") in keep]
    _save(detailed_p, kept)

    summary = _rows(summary_p)
    if summary:
        _save(summary_p, [r for r in summary if r.get("movie") in keep])

    dropped = len({r.get("movie") for r in detailed}) - len(keep)
    print(f"  opening day {date_code}: kept {len(keep)} film(s) "
          f"({', '.join(sorted(keep))}), dropped {dropped} other title(s)")
    return len(kept)


# ---------------------------------------------------------------- debug entry
if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(
        description="Probe District for upcoming releases (no scrape, no publish)."
    )
    ap.add_argument("--window", type=int, default=None, help="days ahead (default 21)")
    ap.add_argument("--quiet", action="store_true", help="less per-date detail")
    a = ap.parse_args()

    # Load .env by hand: python-dotenv is NOT in requirements.txt, so importing
    # it silently failed and every District call went out unauthenticated (401).
    # This must happen BEFORE scraper.fetcher_async is imported — that module
    # reads DISTRICT_* into constants at import time.
    _load_env_file()

    out = discover_opening_days(window_days=a.window, force=True,
                                verbose=not a.quiet)
    print()
    print(json.dumps(out, indent=2))