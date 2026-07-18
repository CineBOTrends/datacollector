"""
Tracked-movie filter, applied DURING the scrape.

Rows for movies that aren't on the tracked list are dropped as soon as each
venue is parsed, so they never reach the shard files, the combined files, R2 or
the dashboard.

Config: tracked_movies.json in the collector root.

    { "mode": "selected", "movies": ["The Odyssey", "Jana Nayagan"] }

    mode "all"       -> keep everything (same as having no file)
    mode "selected"  -> keep only the listed movies

Matching ignores format/language suffixes and case, so "Lenin" matches
"Lenin (2D - Telugu)" and "Lenin (IMAX - Telugu)". Titles or slugs both work.

TRADE-OFF, on purpose: this is destructive. A movie not on the list is never
collected, so adding it later gives you NO back-history — tracking starts from
that moment. (Filtering at build time instead would keep the raw data and stay
reversible, but the files and storage stay full-size.)
"""
import json
import os
import re

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRACK_FILE = os.path.join(HERE, "tracked_movies.json")

_TAG_RE = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]\s*$")
_cache = None


def _canonical(t):
    """'Lenin (2D - Telugu)' -> 'Lenin'"""
    t = (t or "").strip()
    prev = None
    while prev != t and t:
        prev = t
        t = _TAG_RE.sub("", t).strip()
    return t


def _slug(t):
    return re.sub(r"[^a-zA-Z0-9]+", "-", (t or "").lower()).strip("-")


def load_tracked(force=False):
    """Return (mode, keys). Cached — the file is read once per process."""
    global _cache
    if _cache is not None and not force:
        return _cache

    if not os.path.exists(TRACK_FILE):
        _cache = ("all", set())
        return _cache

    try:
        with open(TRACK_FILE, encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception as e:
        print(f"! tracked_movies.json unreadable ({e}) -> tracking ALL")
        _cache = ("all", set())
        return _cache

    mode = str(cfg.get("mode", "selected")).strip().lower()
    if mode not in ("all", "selected"):
        mode = "selected"

    keys = set()
    for entry in cfg.get("movies") or []:
        if not isinstance(entry, str) or not entry.strip():
            continue
        raw = entry.strip()
        keys.add(_canonical(raw).casefold())
        keys.add(_slug(raw))
        keys.add(_canonical(raw.replace("-", " ")).casefold())

    if mode == "selected" and not keys:
        # scraping nothing would produce empty shards and an empty dashboard
        print("! tracked_movies.json is mode=selected but lists no movies "
              "-> tracking ALL (refusing to scrape nothing)")
        mode = "all"

    _cache = (mode, keys)
    return _cache


def is_tracked(title, tracked=None):
    mode, keys = tracked or load_tracked()
    if mode == "all":
        return True
    if not title:
        return False
    c = _canonical(title).casefold()
    return c in keys or _slug(title) in keys or _slug(c) in keys


def filter_rows(rows, logger=None, label=""):
    """Drop rows whose movie isn't tracked. Returns the kept rows."""
    tracked = load_tracked()
    if tracked[0] == "all" or not rows:
        return rows

    kept, dropped = [], set()
    for r in rows:
        title = r.get("movie", "")
        if is_tracked(title, tracked):
            kept.append(r)
        else:
            dropped.add(_canonical(title))

    if logger and dropped:
        logger.info(
            f"Tracked filter{label}: kept {len(kept)}/{len(rows)} rows "
            f"({len(dropped)} untracked title(s) dropped)"
        )
    return kept


def describe():
    mode, keys = load_tracked()
    if mode == "all":
        return "tracking ALL movies"
    titles = sorted({k for k in keys if " " in k or "-" not in k})
    return f"tracking {len(titles)} selected movie(s): {', '.join(titles)}"
