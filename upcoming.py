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


def _base_title(t):
    """'The Odyssey (2D - English)' -> 'the odyssey'.

    District reports one row per format, so the same film arrives as several
    titles. Compare on the base name or the exclusion set leaks.
    """
    return re.sub(r"\s*\([^)]*\)\s*$", "", (t or "")).strip().casefold()


def probe_venues(cities=None, per_city=None):
    """A few District venues in each of the given cities."""
    cities = cities or PROBE_CITIES
    per_city = per_city or PROBE_PER_CITY
    with open(os.path.join("venues", "districtvenues.json"), encoding="utf-8") as f:
        allv = json.load(f)
    picked, seen = [], {c: 0 for c in cities}
    for v in allv:
        city = v.get("city")
        if city in seen and seen[city] < per_city:
            picked.append(v)
            seen[city] += 1
        if all(n >= per_city for n in seen.values()):
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


# A bigger sample for TODAY: a false "not playing today" is the one error that
# matters (it would tag a running film as upcoming), so cast a wider net there.
TODAY_CITIES = ("Hyderabad", "Bengaluru", "Chennai", "Mumbai", "New Delhi",
                "Ahmedabad", "Surat", "Gurgaon")
TODAY_PER_CITY = 6          # ~48 venues


def discover_opening_days(window_days=None, probe_shard=None, force=False,
                          verbose=False):
    """Return {release_date(YYYYMMDD): [titles opening that day]}.

    District's showtime payload carries NO release date — the diagnostic dump
    proved it (keys: censor, contentId, cover, duration, genres, id, isNew,
    label, lang, movieId, name, poster, rating, scrnFmt, sndFmt, thumbnail,
    totalSessionCount, trailer). The date lives only on the movie page, a
    different endpoint. So we infer it from bookings, which is sound:

        not playing TODAY, but has shows on a future date
            => hasn't released yet
            => its opening day is the EARLIEST date it has shows

    A currently-running film always has shows today, so it never qualifies.
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

    # --- what is playing TODAY (the exclusion set) ---
    today_venues = probe_venues(TODAY_CITIES, TODAY_PER_CITY)
    print(f"  probing TODAY across {len(today_venues)} venues (exclusion set)")
    try:
        rows_today = probe_date(ymd(today), today_venues, logger)
    except Exception as e:
        raise RuntimeError(f"today's probe failed ({e}) — refusing to guess "
                           "what is already running") from e

    running = {r.get("movie") for r in rows_today if r.get("movie")}
    running_bases = {_base_title(t) for t in running}
    print(f"  {len(running)} title(s) playing today -> excluded")
    if verbose:
        for t in sorted(running_bases):
            print(f"      running: {t}")

    # --- future dates ---
    venues = probe_venues()
    print(f"  probing D+1..D+{window_days} across {len(venues)} venues")

    first_seen = {}          # title -> earliest future date with shows
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
            if not title or title in first_seen:
                continue
            if title in running or _base_title(title) in running_bases:
                continue                      # already out
            first_seen[title] = d
            hits.append(title)

        if verbose and hits:
            print(f"    {dc}: NEW -> {', '.join(sorted(hits))}")

    opening = {}
    for title, d in first_seen.items():
        opening.setdefault(ymd(d), []).append(title)
    for k in opening:
        opening[k] = sorted(opening[k])

    if opening:
        print("  opening days found:")
        for dc, titles in sorted(opening.items()):
            print(f"    {dc}: {', '.join(titles)}")
    else:
        print("    no upcoming releases with open bookings in the window")

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
    keep_bases = {_base_title(t) for t in titles}
    # match on the base name: District emits one title per format
    # ("The Odyssey (2D - English)", "(4DX-2D - Hindi)", ...)
    kept = [r for r in detailed
            if r.get("movie") in keep or _base_title(r.get("movie")) in keep_bases]
    _save(detailed_p, kept)

    summary = _rows(summary_p)
    if summary:
        _save(summary_p, [r for r in summary
                          if r.get("movie") in keep
                          or _base_title(r.get("movie")) in keep_bases])

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