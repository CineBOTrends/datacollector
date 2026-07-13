#!/usr/bin/env python3
"""
Upcoming-release tracking.

The regular advance scrape only looks at D+1. Bookings for a NEW film, though,
open days before it lands — so this module runs the same advance scrape at
D+3 / D+5 / D+7 and then throws away everything that is already running.

Why the filter matters: a D+3 scrape returns shows for currently-running films
too (people book those 3 days out as well). Without filtering, the "upcoming"
feed would just be a duplicate of advance. A film counts as UPCOMING when its
release date is still in the future.

Output lands in the normal advance tree (advance/data/<date_code>/), so the
dashboard picks the dates up as advance chips with no changes at all.
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

DEFAULT_OFFSETS = [3, 5, 7]

# Same aliases build_data.py probes — District's movieInfo is inconsistent.
_RELEASE_KEYS = ("releaseDate", "releasedate", "release_date", "releaseDateText",
                 "release", "releaseOn", "releasedOn")


def today_ist():
    return datetime.now(IST).date()


def date_code_for(offset):
    return (datetime.now(IST) + timedelta(days=offset)).strftime("%Y%m%d")


def _parse_release(info):
    """Return a date object from a movieInfo block, or None."""
    for k in _RELEASE_KEYS:
        v = info.get(k)
        if v in (None, "", 0):
            continue
        if isinstance(v, (int, float)):
            try:
                ts = float(v)
                if ts > 1e12:                     # milliseconds
                    ts /= 1000.0
                return datetime.utcfromtimestamp(ts).date()
            except (ValueError, OverflowError, OSError):
                continue
        s = str(v).strip()
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
        if m:
            try:
                return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date()
            except ValueError:
                continue
    return None


def release_dates_by_movie(rows):
    """movie title -> release date (first non-empty movieInfo we see)."""
    out = {}
    for r in rows:
        title = r.get("movie")
        if not title or title in out:
            continue
        mi = r.get("movieInfo")
        if not mi:
            continue
        d = _parse_release(mi)
        if d:
            out[title] = d
    return out


def _load(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def filter_to_upcoming(date_code, running_titles=None):
    """Trim a combined advance date down to not-yet-released films.

    A title is kept when its release date is in the future. Titles with no
    release date are kept only if they are NOT playing today (i.e. absent from
    today's daily feed) — an unknown release date on a film that's already
    running means it is not upcoming.
    """
    base = os.path.join("advance", "data", date_code)
    detailed_p = os.path.join(base, "finaldetailed.json")
    summary_p = os.path.join(base, "finalsummary.json")

    detailed = _load(detailed_p)
    if not detailed:
        print(f"  ! nothing combined for {date_code}, skipping filter")
        return 0, 0

    today = today_ist()
    releases = release_dates_by_movie(detailed)
    running = running_titles if running_titles is not None else titles_playing_today()

    keep, drop = set(), set()
    for title in {r.get("movie") for r in detailed if r.get("movie")}:
        rel = releases.get(title)
        if rel is not None:
            (keep if rel > today else drop).add(title)
        else:
            (drop if title in running else keep).add(title)

    kept_rows = [r for r in detailed if r.get("movie") in keep]
    _save(detailed_p, kept_rows)

    summary = _load(summary_p)
    if summary:
        _save(summary_p, [r for r in summary if r.get("movie") in keep])

    _save(os.path.join(base, "upcoming_movies.json"), sorted(
        [{"movie": t, "releaseDate": releases[t].isoformat() if t in releases else None}
         for t in keep],
        key=lambda x: (x["releaseDate"] or "9999-99-99", x["movie"]),
    ))

    print(f"  upcoming {date_code}: kept {len(keep)} film(s), dropped {len(drop)} already-running")
    for t in sorted(keep):
        rel = releases.get(t)
        print(f"      + {t}" + (f"  (releases {rel})" if rel else "  (release date unknown)"))
    return len(keep), len(kept_rows)


def titles_playing_today():
    """Titles in today's daily feed — these are already released."""
    dc = datetime.now(IST).strftime("%Y%m%d")
    rows = _load(os.path.join("daily", "data", dc, "finalsummary.json"))
    if not rows:
        rows = _load(os.path.join("daily", "data", dc, "finaldetailed.json"))
    return {r.get("movie") for r in rows if r.get("movie")}


def offsets_from_config():
    """Read `upcoming.offsets` from schedule_config.yaml, else the default."""
    try:
        import yaml
        with open(os.path.join(PROJECT_ROOT, "schedule_config.yaml"), "r") as f:
            cfg = yaml.safe_load(f) or {}
        offs = ((cfg.get("upcoming") or {}).get("offsets")) or DEFAULT_OFFSETS
        return [int(o) for o in offs]
    except Exception:
        return list(DEFAULT_OFFSETS)
