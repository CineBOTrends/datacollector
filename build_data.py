#!/usr/bin/env python3
"""
CineBOTrends - data builder
===========================
Turns the (private) data-collector output into the compact JSON the (public)
dashboard reads at runtime.

It NEVER mutates the collector. It reads:

    <collector>/<mode>/data/<YYYYMMDD>/finaldetailed.json   (show-level rows)
    <collector>/<mode>/data/<YYYYMMDD>/finalsummary.json    (optional, fallback)

and writes, into ./data/ :

    data/manifest.json                       modes, dates, run schedule
    data/<mode>/<date>/national.json         home grid + hero KPIs (all movies)
    data/<mode>/<date>/m/<slug>.json         full drill-down for one movie
    data/<mode>/history/<slug>.json          day/city/state/format-wise history

Run it whenever the collector produces new data:

    python3 build_data.py /path/to/datacollector

Daily mode lights up automatically once the collector emits daily/data/* folders.
"""

import json, os, re, sys, shutil
import datetime as _dt
from collections import defaultdict

# Reused to compute a PER-MOVIE territory-wise breakdown (same shape as the
# global territory_tracked.json), so a movie's own "All India Report" tab
# shows just that movie's numbers instead of every tracked movie combined.
# Loaded lazily/defensively: if territory_report.py or its config is missing
# or broken, movies simply get no "territory" key rather than failing the
# whole build.
try:
    from territory_report import (
        load_config as _t_load_config,
        build_groups as _t_build_groups,
        _aggregate as _t_aggregate,
        _compute_groups as _t_compute_groups,
        _occ as _t_occ,
        DEFAULT_GROUPS as _T_DEFAULT_GROUPS,
    )
    _TERRITORY_IMPORT_ERROR = None
except Exception as _e:
    _TERRITORY_IMPORT_ERROR = _e

MODES = {
    "advance": {"label": "Advance", "runsPerDay": 6,
                "runTimes": ["08:45", "11:45", "14:45", "17:45", "20:45", "23:30"]},
    "daily":   {"label": "Daily", "runsPerDay": 13,
                "runTimes": ["03:00", "05:00", "07:00", "08:00", "10:00", "11:00",
                             "13:00", "14:00", "16:00", "17:00", "19:00", "20:00", "22:00"]},
}

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "data")              # FULL tree (admin)
# Mirrored poster images; the publish step copies this to <dashboard>/assets/posters
POSTER_DIR = os.path.join(HERE, "poster_assets")
POSTER_URL_PREFIX = "/assets/posters/"
BMS_BG_URL = "https://assets-in.bmscdn.com/iedb/movies/images/mobile/listing/xxlarge/{id}.jpg"
_poster_done = {}


def _download(url, dest):
    """Download url -> dest. True on success; an existing file is left untouched on failure."""
    import urllib.request
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Referer": "https://in.bookmyshow.com/",
        })
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = resp.read()
        if len(data) < 1000:
            return False
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        tmp = dest + ".part"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, dest)
        return True
    except Exception as e:
        print(f"    ! poster download failed {url} ({e})")
        return False


BMS_THUMB_URL = "https://assets-in.bmscdn.com/iedb/movies/images/mobile/thumbnail/xlarge/{id}.jpg"


def load_bms_codes(collector=None):
    """tracked_movies.json "bms_codes": {"Jailer 2": "ET00xxxxxx"} (code or BMS
    movie URL). Lets a title's posters be fetched BEFORE any show exists.
    Returns {canon_key(title): "ETxxxxxxxx"}."""
    cfg = None
    for root in [c for c in (collector, HERE) if c]:
        fp = os.path.join(root, TRACK_FILE)
        if os.path.exists(fp):
            try:
                with open(fp, encoding="utf-8") as f:
                    cfg = json.load(f)
                break
            except Exception:
                pass
    out = {}
    for title, val in ((cfg or {}).get("bms_codes") or {}).items():
        m = re.search(r"ET\d{8}", str(val or ""), re.I)
        if m and not str(title).startswith("_"):
            out[canon_key(title)] = m.group(0).upper()
    return out


def _bms_image_from_rows(rows, extra_codes=(), title=None):
    """Resolve the BMS image id from the BMS event codes on a movie's rows
    (most common code first), then any code configured in tracked_movies.json,
    then (if still none) a code auto-discovered from BMS listings by title."""
    from collections import Counter
    codes = [c for c, _ in Counter(r["bmsCode"] for r in rows if r.get("bmsCode")).most_common()]
    codes += [c for c in extra_codes if c not in codes]
    try:
        from scraper.bms_poster import resolve_image_id, find_code
    except Exception:
        return None
    if not codes and title:
        auto = find_code(title)
        if auto:
            codes.append(auto)
    for code in codes:
        img = resolve_image_id(code)
        if img:
            return img
    return None


def prefetch_tracked_posters(collector, built_slugs):
    """Fetch posters for tracked titles that have no shows yet (bookings not
    open). The BMS code comes from tracked_movies.json "bms_codes" if given,
    otherwise it is discovered automatically from BMS listings by title."""
    codes = load_bms_codes(collector)
    try:
        with open(os.path.join(collector or HERE, TRACK_FILE), encoding="utf-8") as f:
            titles = json.load(f).get("movies") or []
    except Exception:
        return
    title_map = load_title_mapping()
    for entry in titles:
        raw = entry.get("title") if isinstance(entry, dict) else entry
        raw = str(raw or "").strip()
        if not raw:
            continue
        slug = slugify(canonical_title(title_map.get(canon_key(raw)) or raw))
        if slug in built_slugs:
            continue
        if all(os.path.exists(os.path.join(POSTER_DIR, f"{slug}-{k}.jpg")) for k in ("thumb", "bg")):
            continue
        code = codes.get(canon_key(raw))
        img = _bms_image_from_rows([], [code] if code else [], title=raw)
        if not img:
            print(f"    ! no BMS poster found for {raw} (not listed on BMS yet? "
                  f"add its code under bms_codes in {TRACK_FILE})")
            continue
        mirror_posters(slug, None, img)
        print(f"  poster (pre-booking): {raw} -> {slug}")


def mirror_posters(slug, poster, bms_id):
    """Mirror thumb (District, else BMS thumbnail) + bg (BMS xxlarge, else District)
    to POSTER_DIR and return the poster dict pointing at
    /assets/posters/<slug>-{thumb,bg}.jpg where mirrored."""
    if (slug, bms_id) in _poster_done:
        return _poster_done[(slug, bms_id)]
    out = dict(poster) if poster else {}
    thumb_url = (poster or {}).get("thumb") or (BMS_THUMB_URL.format(id=bms_id) if bms_id else None)
    bg_url = BMS_BG_URL.format(id=bms_id) if bms_id else (poster or {}).get("bg")
    for kind, url in (("thumb", thumb_url), ("bg", bg_url)):
        if not url:
            continue
        dest = os.path.join(POSTER_DIR, f"{slug}-{kind}.jpg")
        if _download(url, dest) or os.path.exists(dest):
            out[kind] = f"{POSTER_URL_PREFIX}{slug}-{kind}.jpg"
    result = out or None
    _poster_done[(slug, bms_id)] = result
    return result

KEY_RE = re.compile(r"^(.*)\s\(([^)]*?)\s-\s([^)]*)\)\s*$")

# BookMyShow image CDN pattern.
#   id = "<title-slug>-<event-code>-<timestamp>"  e.g. balaramana-dinagalu-et00478884-1782106150

def _fmt_runtime(mins):
    try:
        mins = int(mins)
    except (TypeError, ValueError):
        return None
    if mins <= 0:
        return None
    return f"{mins // 60}h {mins % 60:02d}m"


# District/BMS movieInfo blocks are inconsistent about field names, so probe
# several aliases when pulling out release date and cast.
_RELEASE_KEYS = ("releaseDate", "releasedate", "release_date", "releaseDateText",
                 "release", "releaseOn", "releasedOn")
_CAST_KEYS = ("cast", "casts", "actors", "starCast", "starcast", "castList",
              "castCrew", "castAndCrew")


def _extract_release_date(info):
    """Return an ISO-ish 'YYYY-MM-DD' release date from a movieInfo block, or None."""
    for k in _RELEASE_KEYS:
        v = info.get(k)
        if v in (None, "", 0):
            continue
        if isinstance(v, (int, float)):
            # epoch seconds or milliseconds
            try:
                ts = float(v)
                if ts > 1e12:            # milliseconds
                    ts /= 1000.0
                return _dt.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")
            except (ValueError, OverflowError, OSError):
                continue
        s = str(v).strip()
        if s:
            return s[:10] if len(s) >= 10 and s[4:5] == "-" else s
    return None


def _extract_cast(info):
    """Return a list of cast-member name strings from a movieInfo block."""
    for k in _CAST_KEYS:
        raw = info.get(k)
        if not raw:
            continue
        if isinstance(raw, str):
            parts = [p.strip() for p in re.split(r"[,|;]", raw)]
            names = [p for p in parts if p]
        elif isinstance(raw, list):
            names = []
            for p in raw:
                if isinstance(p, str):
                    nm = p.strip()
                elif isinstance(p, dict):
                    nm = str(p.get("name") or p.get("Name") or p.get("personName")
                             or p.get("title") or "").strip()
                else:
                    nm = ""
                if nm:
                    names.append(nm)
        else:
            names = []
        if names:
            return names
    return []


def district_meta_from_rows(rows):
    """Derive {poster, meta} from the District worker's movieInfo embedded in rows.

    NOTE: releaseDate is deliberately NOT read from District's movieInfo here
    (District's own field is unreliable / frequently wrong or missing). The
    user now maintains release dates by hand in tracked_movies.json, and
    that's the ONLY source meta.releaseDate is ever set from — see
    load_tracked_release_dates() and its stamping pass in build_date().
    """
    best = None
    fallback = None
    cast = []
    for r in rows:
        mi = r.get("movieInfo")
        if not mi:
            continue
        # cast may live on a different row than the poster — keep the first
        # non-empty value we find across all rows.
        if not cast:
            cast = _extract_cast(mi)
        if fallback is None and (mi.get("poster") or mi.get("genres") or mi.get("censor")
                                 or mi.get("duration") or mi.get("trailer")):
            fallback = mi
        if mi.get("poster"):
            best = mi
            if cast:
                break
    info = best or fallback
    if not info:
        return {"poster": None, "meta": None}

    thumb = (info.get("poster") or info.get("thumbnail") or "").strip()
    bg = (info.get("cover") or info.get("poster") or "").strip()
    poster = {"thumb": thumb or bg, "bg": bg or thumb} if (thumb or bg) else None

    lang = (info.get("lang") or "").strip()
    meta = {
        "genres": info.get("genres") or [],
        "runTime": _fmt_runtime(info.get("duration")),
        "certification": (info.get("censor") or "").strip() or None,
        "languages": [lang] if lang else [],
        "likes": None,
        "eventCode": (str(info["contentId"]) if info.get("contentId") is not None else None),
        "releaseDate": None,
        "cast": cast,
        "trailer": (info.get("trailer") or "").strip() or None,
    }
    return {"poster": poster, "meta": meta}


# Trailing "(2003)" / "[3D]" style tags, stripped so title variants merge.
_TITLE_TAG_RE = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]\s*$")


def canonical_title(base):
    """Strip trailing version/year/format tags so BMS and District titles merge.
    'Okkadu (2003)' -> 'Okkadu';  'Hanu-Man [3D]' -> 'Hanu-Man'."""
    t = (base or "").strip()
    prev = None
    while prev != t and t:
        prev = t
        t = _TITLE_TAG_RE.sub("", t).strip()
    return t or (base or "").strip()


# ============================================================
# SELECTED-MOVIE TRACKING
#
# Filtering happens HERE, at build time — not at scrape or combine time.
# Two reasons:
#   1. It saves nothing to filter earlier. The scrape cost is per VENUE (one
#      API call returns every movie playing there), so skipping a movie does
#      not skip any work.
#   2. It stays reversible. The raw + combined files in R2 keep every movie,
#      so adding a title to the list later rebuilds its FULL history. Filter
#      at combine time and that history is gone for good.
#
# tracked_movies.json (collector root, optional):
#   { "mode": "selected",
#     "movies": ["The Odyssey", "Lenin", "jana-nayagan"] }
#
# mode "all" (or no file at all) = track everything, i.e. current behaviour.
# ============================================================
TRACK_FILE = "tracked_movies.json"
_track_cache = None


def load_tracked(collector=None):
    """Return (mode, keyset). mode is 'all' or 'selected'."""
    global _track_cache
    if _track_cache is not None:
        return _track_cache

    roots = [HERE]
    if collector:
        roots.insert(0, collector)

    cfg = None
    for root in roots:
        fp = os.path.join(root, TRACK_FILE)
        if os.path.exists(fp):
            try:
                with open(fp, encoding="utf-8") as f:
                    cfg = json.load(f)
                print(f"  tracking list: {fp}")
                break
            except Exception as e:
                print(f"    ! {TRACK_FILE} in {root} ignored ({e})")

    if not cfg:
        _track_cache = ("all", set())
        return _track_cache

    mode = str(cfg.get("mode", "selected")).strip().lower()
    if mode not in ("all", "selected"):
        mode = "selected"

    # hide_untracked controls what happens to movies ALREADY collected:
    #   true  (default) - the dashboard shows only the tracked list; everything
    #                     else vanishes from the site (raw data is untouched, so
    #                     removing the list brings it all back)
    #   false           - already-collected movies stay on the site; the list
    #                     only controls what is NEWLY scraped from now on
    if mode == "selected" and cfg.get("hide_untracked") is False:
        print("  hide_untracked=false -> keeping already-collected movies "
              "visible; the list only limits NEW scraping")
        _track_cache = ("all", set())
        return _track_cache

    keys = set()
    for entry in cfg.get("movies", []) or []:
        # A "movies" entry is normally just a title/slug string, but it can
        # also be an object -- {"title": "...", "releaseDate": "..."} -- so a
        # user-provided release date (see load_tracked_release_dates() below)
        # can live right next to the title it corrects, instead of a second
        # file to keep in sync. Either form counts for tracking purposes.
        if isinstance(entry, dict):
            raw = str(entry.get("title") or entry.get("movie") or entry.get("name") or "").strip()
        elif isinstance(entry, str):
            raw = entry.strip()
        else:
            raw = ""
        if not raw:
            continue
        # accept a title ("The Odyssey"), a slug ("the-odyssey") or either case
        keys.add(canon_key(raw))
        keys.add(slugify(raw))
        keys.add(canon_key(raw.replace("-", " ")))

    if mode == "selected" and not keys:
        # an empty list would publish an EMPTY dashboard — refuse and track all
        print("    ! tracked_movies.json is mode=selected but lists no movies "
              "-> tracking ALL (refusing to build an empty site)")
        mode = "all"

    _track_cache = (mode, keys)
    return _track_cache


_title_map_cache = None


def load_title_mapping(collector=None):
    """Read tracked_movies.json's optional "title_mapping":
        {"Thella Kaagitham": "Thellakaagitham"}
    i.e. {canonical_title: alias}, for a film BMS and District scrape under
    two different spellings/spacings of the SAME title -- canon_key() alone
    can't catch this, since it only strips trailing format/version tags, it
    never fixes spelling/spacing differences mid-title. Without this, the two
    sources' rows build as two separate, each-incomplete "movies" (and
    whichever spelling isn't in tracked_movies.json's "movies"/"release_dates"
    list gets silently filtered out of the dashboard build entirely, even
    though it was scraped and stored fine).

    Returns {canon_key(alias): canonical_title}, so a raw scraped title can
    be looked up and rewritten to the canonical spelling BEFORE grouping,
    tracking-filter matching, or anything else keys off of it.
    """
    global _title_map_cache
    if _title_map_cache is not None:
        return _title_map_cache

    roots = [HERE]
    if collector:
        roots.insert(0, collector)

    cfg = None
    for root in roots:
        fp = os.path.join(root, TRACK_FILE)
        if os.path.exists(fp):
            try:
                with open(fp, encoding="utf-8") as f:
                    cfg = json.load(f)
                break
            except Exception:
                pass

    mapping = {}
    for canonical, alias in (cfg or {}).get("title_mapping", {}).items():
        canonical = str(canonical or "").strip()
        alias = str(alias or "").strip()
        if not canonical or not alias:
            continue
        mapping[canon_key(alias)] = canonical

    _title_map_cache = mapping
    return _title_map_cache


_track_release_cache = None


def load_tracked_release_dates(collector=None):
    """Read tracked_movies.json again for any per-movie release date the
    user has provided directly. Two forms are accepted:

        {"release_dates": {"The Paradise": "2026-09-25"}}            <- primary
        {"movies": [{"title": "The Paradise", "releaseDate": "..."}]} <- also OK

    Returns {canon_key_or_slug(title): "YYYY-MM-DD"}.

    This is a user-curated fact, not a guess, so it's the FIRST thing
    checked when stamping meta.releaseDate onto a movie's files -- ahead of
    District's own (often missing/wrong) release-date field and the
    inferred "first daily date" fallback below. It's also the only way to
    fix a film whose release date build_data can't infer at all (e.g. one
    that's never appeared in `daily` yet and had no distinguishable
    premiere/advance split).
    """
    global _track_release_cache
    if _track_release_cache is not None:
        return _track_release_cache

    roots = [HERE]
    if collector:
        roots.insert(0, collector)

    cfg = None
    for root in roots:
        fp = os.path.join(root, TRACK_FILE)
        if os.path.exists(fp):
            try:
                with open(fp, encoding="utf-8") as f:
                    cfg = json.load(f)
                break
            except Exception:
                pass

    cfg = cfg or {}
    dates = {}

    def _store(title, raw):
        title = str(title or "").strip()
        raw = str(raw or "").strip()
        if not title or not raw:
            return
        iso = raw[:10] if len(raw) >= 10 and raw[4:5] == "-" else raw
        dates[canon_key(title)] = iso
        dates[slugify(title)] = iso

    # Primary form: a top-level {"release_dates": {title_or_slug: date}} map.
    for title, raw in (cfg.get("release_dates") or {}).items():
        _store(title, raw)

    # Also accept a per-entry releaseDate inside "movies", for anyone who
    # prefers keeping it next to the title instead of a separate map.
    for entry in cfg.get("movies", []) or []:
        if not isinstance(entry, dict):
            continue
        title = entry.get("title") or entry.get("movie") or entry.get("name")
        _store(title, _extract_release_date(entry))

    _track_release_cache = dates
    return _track_release_cache


def is_tracked(title, slug, tracked):
    """Does this movie match the tracked list? Title OR slug, either form."""
    mode, keys = tracked
    if mode == "all":
        return True
    return (
        canon_key(title) in keys
        or slug in keys
        or canon_key(slug.replace("-", " ")) in keys
    )


def canon_key(base):
    return canonical_title(base).casefold()


def _first_word_key(title):
    """First significant word of a title, for max-coverage matching.
    'Okkadu (2003)' -> 'okkadu';  'Hanu-Man [3D]' -> 'hanu-man'."""
    t = canonical_title(title).casefold()
    parts = t.split()
    tok = parts[0] if parts else t
    return re.sub(r"[^0-9a-z]", "", tok)   # strip punctuation: hanu-man == HanuMan


def parse_key(key):
    """'Peddi (4DX - Telugu)' -> ('Peddi', '4DX', 'Telugu')."""
    m = KEY_RE.match(key.strip())
    if not m:
        return key.strip(), "", ""
    return m.group(1).strip(), m.group(2).strip(), m.group(3).strip()


def slugify(title):
    s = re.sub(r"[^a-zA-Z0-9]+", "-", title.lower()).strip("-")
    return s or "movie"


def norm_format(fmt):
    """Bucket a raw format token into the spec's summary categories."""
    f = fmt.upper()
    if "IMAX" in f:            return "IMAX"
    if "MX4D" in f:            return "4DX"
    if "4DX" in f:             return "4DX"
    if "ICE" in f:             return "ICE"
    if "DOLBY CINEMA" in f:    return "Dolby Cinema"
    if "7D" in f:              return "Others"
    if "3D" in f:              return "3D"
    if "2D" in f:              return "2D"
    return "Others" if f else "2D"


def occ(sold, seats):
    return round(sold / seats * 100, 2) if seats else 0.0


def row_vals(r):
    """Read seats/sold/gross tolerant of both collector schemas:
    advance/combined rows use ticketsSold/grossRevenue; District/daily rows use sold/gross."""
    seats = r.get("totalSeats") or 0
    sold = r.get("ticketsSold")
    if sold is None:
        sold = r.get("sold")
    gross = r.get("grossRevenue")
    if gross is None:
        gross = r.get("gross")
    return int(seats or 0), int(sold or 0), float(gross or 0.0)


def blank(**extra):
    d = {"gross": 0.0, "sold": 0, "seats": 0, "shows": 0,
         "housefull": 0, "fastfilling": 0}
    d.update(extra)
    return d


def add_show(acc, sold, seats, gross):
    acc["gross"] += gross
    acc["sold"] += sold
    acc["seats"] += seats
    acc["shows"] += 1
    o = occ(sold, seats)
    if o >= 98:
        acc["housefull"] += 1
    elif o >= 50:
        acc["fastfilling"] += 1


def finalize(acc):
    acc["gross"] = round(acc["gross"], 2)
    acc["occupancy"] = occ(acc["sold"], acc["seats"])
    return acc


# --------------------------------------------------------------------------- #
#  Cross-source theatre de-duplication (shared with territory_report.py —
#  see dedupe_theatres.py). Applied once per movie's row group in
#  build_date() below, BEFORE build_movie() / _movie_territory() ever see the
#  rows, so kpi, State/City/Format Wise, and the All India Report tab are
#  always built from the exact same de-duplicated rows and can't drift apart.
# --------------------------------------------------------------------------- #
from dedupe_theatres import dedupe_theatre_rows as _dedupe_theatre_rows


# --------------------------------------------------------------------------- #
#  Per-movie aggregation
# --------------------------------------------------------------------------- #
_CITY_STATE = None


def _city_state(city):
    """City -> state, for rows whose venue record carried no state."""
    global _CITY_STATE
    if _CITY_STATE is None:
        fp = os.path.join(HERE, "venues", "city_state.json")
        try:
            with open(fp, encoding="utf-8") as f:
                _CITY_STATE = json.load(f)
            print(f"  city->state map: {len(_CITY_STATE)} cities")
        except Exception:
            _CITY_STATE = {}
    return _CITY_STATE.get(" ".join(str(city).split()).casefold(), "Unknown")


def _load_territory_context():
    """Load territory_config.json + build the city/state index ONCE per
    build_date() call (not per movie — building the index re-parses every
    territory/*.json file, which is wasteful to redo for each movie)."""
    if _TERRITORY_IMPORT_ERROR is not None:
        print(f"    ! territory_report import failed, no per-movie "
              f"territory breakdown this build: {_TERRITORY_IMPORT_ERROR}")
        return None
    try:
        cfg = _t_load_config()
        return {
            "groups": _t_build_groups(cfg),
            "rest_key": cfg.get("rest_of_india_key", "rest-of-india"),
            "rest_label": cfg.get("rest_of_india_label", "Rest of India"),
            "order": cfg.get("territory_order", []),
            "group_defs": cfg.get("groups", _T_DEFAULT_GROUPS),
        }
    except Exception as e:
        print(f"    ! territory_config.json unusable, no per-movie "
              f"territory breakdown this build: {e}")
        return None


def _movie_territory(rows, ctx):
    """Same {totals, territories, groups} shape as territory.json /
    territory_tracked.json, but aggregated over just this movie's rows."""
    if ctx is None:
        return None
    territories, gross, shows, sold, seats, stats_by_key = _t_aggregate(
        rows, ctx["groups"], ctx["rest_key"], ctx["rest_label"], ctx["order"]
    )
    groups = _t_compute_groups(
        territories, stats_by_key, ctx["group_defs"], ctx["rest_key"], ctx["rest_label"]
    )
    return {
        "totals": {
            "territories": len(territories),
            "gross": round(gross, 2),
            "shows": shows,
            "occupancy": _t_occ(sold, seats),
        },
        "territories": territories,
        "groups": groups,
    }


def build_movie(title, rows):
    """rows: every show row whose base title == `title`."""
    languages, formats = set(), set()
    fmt_acc = defaultdict(blank)
    lang_acc = defaultdict(blank)
    states = {}          # state -> {agg, cities{city -> {agg, theatres{venue->{agg, shows[]}}}}}

    # movie-level avg price (for maxGross of zero-sold shows)
    tot_sold = tot_gross = 0
    for r in rows:
        _, s, g = row_vals(r)
        tot_sold += s
        tot_gross += g
    avg_price = (tot_gross / tot_sold) if tot_sold else 0.0

    for r in rows:
        _, raw_fmt, lang = parse_key(r["movie"])
        fmt = norm_format(raw_fmt)
        if lang:
            languages.add(lang)
        formats.add(fmt)

        seats, sold, gross = row_vals(r)
        o = occ(sold, seats)
        price = (gross / sold) if sold else avg_price
        max_gross = round(price * seats, 2)

        add_show(fmt_acc[fmt], sold, seats, gross)
        if lang:
            add_show(lang_acc[lang], sold, seats, gross)

        city = (r.get("city") or "Unknown").strip() or "Unknown"
        state = (r.get("state") or "").strip()
        if not state or state == "Unknown":
            # Applied here as well as in the parser so ALREADY-COLLECTED data is
            # corrected on the next build — otherwise every row scraped before
            # the fix would stay stuck under a bogus "Unknown" state forever.
            state = _city_state(city)
        venue = (r.get("venue") or "Unknown").strip() or "Unknown"

        st = states.setdefault(state, {"agg": blank(venues=set()),
                                       "cities": {}})
        add_show(st["agg"], sold, seats, gross)
        st["agg"]["venues"].add(venue)

        ct = st["cities"].setdefault(city, {"agg": blank(venues=set()),
                                            "theatres": {}})
        add_show(ct["agg"], sold, seats, gross)
        ct["agg"]["venues"].add(venue)

        th = ct["theatres"].setdefault(venue, {
            "agg": blank(), "chain": r.get("chain") or "",
            "address": r.get("address") or "", "shows": []})
        add_show(th["agg"], sold, seats, gross)
        th["shows"].append({
            "time": r.get("time") or "",
            "audi": r.get("audi") or "",
            "format": fmt,
            "totalSeats": seats,
            "sold": sold,
            "available": r.get("available", max(seats - sold, 0)),
            "occupancy": o,
            "estimatedCollection": round(gross, 2),
            "maxGross": max_gross,
            "housefull": o >= 98,
            "fastfilling": 50 <= o < 98,
        })

    # ---- shape the nested output, sorted by gross descending ----------------
    out_states = []
    for sname, sd in states.items():
        cities_out = []
        for cname, cd in sd["cities"].items():
            theatres_out = []
            for vname, td in cd["theatres"].items():
                td["shows"].sort(key=lambda s: s["time"])
                theatres_out.append({
                    "venue": vname, "chain": td["chain"], "address": td["address"],
                    **finalize(td["agg"]),
                    "theatres": 1,
                    "showTimings": td["shows"],
                })
            theatres_out.sort(key=lambda t: t["gross"], reverse=True)
            agg = cd["agg"]; nv = len(agg.pop("venues"))
            cities_out.append({
                "city": cname, "theatres": nv, **finalize(agg),
                "theatreList": theatres_out,
            })
        cities_out.sort(key=lambda c: c["gross"], reverse=True)
        agg = sd["agg"]; nv = len(agg.pop("venues"))
        out_states.append({
            "state": sname, "theatres": nv, "cities": len(cities_out),
            **finalize(agg), "cityList": cities_out,
        })
    out_states.sort(key=lambda s: s["gross"], reverse=True)

    fmt_summary = [{"format": k, **finalize(v)} for k, v in fmt_acc.items()]
    fmt_summary.sort(key=lambda x: x["gross"], reverse=True)
    lang_summary = [{"language": k, **finalize(v)} for k, v in lang_acc.items()]
    lang_summary.sort(key=lambda x: x["gross"], reverse=True)

    total = blank()
    for r in rows:
        seats, sold, gross = row_vals(r)
        add_show(total, sold, seats, gross)
    finalize(total)

    cities_total = sum(s["cities"] for s in out_states)
    theatres_total = sum(s["theatres"] for s in out_states)

    return {
        "title": title,
        "slug": slugify(title),
        "languages": sorted(languages),
        "formats": [f["format"] for f in fmt_summary],
        "kpi": {
            "cities": cities_total,
            "gross": total["gross"],
            "sold": total["sold"],
            "shows": total["shows"],
            "theatres": theatres_total,
            "states": len(out_states),
            "seats": total["seats"],
            "occupancy": total["occupancy"],
            "fastfilling": total["fastfilling"],
            "housefull": total["housefull"],
        },
        "formatSummary": fmt_summary,
        "languageSummary": lang_summary,
        "states": out_states,
    }


# --------------------------------------------------------------------------- #
#  Public (trimmed) shaping — NO area / theatre / showtime data
#  Keeps: national, state TOTALS, top-20 cities by gross, language, format,
#  per-movie aggregate pages. The granular data simply isn't written.
# --------------------------------------------------------------------------- #
PUBLIC_TOP_CITIES = 20




def build_date(mode, date, src_dir, out_dir):
    detailed = os.path.join(src_dir, "finaldetailed.json")
    if not os.path.exists(detailed):
        print(f"    ! {mode}/{date}: finaldetailed.json missing, skipping")
        return None

    with open(detailed, encoding="utf-8") as f:
        payload = json.load(f)
    rows = payload.get("data", [])
    last_updated = payload.get("last_updated", "")

    title_map = load_title_mapping()
    grouped = defaultdict(list)
    display = {}                       # canon key -> clean display title
    for r in rows:
        base, fmt, lang = parse_key(r.get("movie", ""))
        mapped = title_map.get(canon_key(base))
        if mapped:
            base = mapped
            # Keep the row's own "movie" field in sync too, so everything
            # downstream that reads r["movie"] directly (the per-movie
            # breakdown inside build_movie/_movie_territory, combiner's
            # cross-source dedup, etc.) sees one consistent spelling rather
            # than treating BMS's and District's titles as different films.
            r["movie"] = f"{base} ({fmt} - {lang})" if (fmt or lang) else base
        ck = canon_key(base)
        grouped[ck].append(r)
        disp = canonical_title(base)
        if ck not in display or (disp and len(disp) < len(display[ck])):
            display[ck] = disp

    m_dir = os.path.join(out_dir, "m")
    os.makedirs(m_dir, exist_ok=True)

    # Global District poster index: collect every poster the District worker
    # provided (across ALL movies), keyed by canonical + first-word title.
    # Lets a District poster apply to the BMS-titled version of the same film.
    global_dposters = {}
    for _r in rows:
        _mi = _r.get("movieInfo")
        if not _mi:
            continue
        _pu = (_mi.get("poster") or _mi.get("thumbnail") or _mi.get("cover") or "").strip()
        if not _pu:
            continue
        _p = {"thumb": _pu, "bg": (_mi.get("cover") or _pu).strip() or _pu}
        _nm = _mi.get("name") or ""
        if _nm:
            global_dposters.setdefault("canon:" + canonical_title(_nm).casefold(), _p)
            _fw = _first_word_key(_nm)
            if _fw:
                global_dposters.setdefault("fw:" + _fw, _p)

    def _global_district_poster(title):
        return (
            global_dposters.get("canon:" + canonical_title(title).casefold())
            or global_dposters.get("fw:" + _first_word_key(title))
        )

    tracked = load_tracked()
    _bms_codes = load_bms_codes()
    territory_ctx = _load_territory_context()

    index = []
    movies_for_history = {}
    used_slugs = set()
    skipped = 0
    for ck, mrows in grouped.items():
        title = display.get(ck) or ck
        # De-dupe cross-source theatre rows (BMS + District both scraping the
        # same physical venue under different name strings) ONCE here, so
        # kpi/state/city/format AND the "territory" (All India Report) block
        # built just below are guaranteed to come from the identical row set.
        mrows = _dedupe_theatre_rows(mrows)
        movie = build_movie(title, mrows)
        slug = movie["slug"]
        # not on the tracked list -> don't publish it (raw data is untouched)
        if not is_tracked(title, slug, tracked):
            skipped += 1
            continue
        try:
            movie["territory"] = _movie_territory(mrows, territory_ctx)
        except Exception as e:
            print(f"    ! territory breakdown failed for {title!r}: {e}")
            movie["territory"] = None
        if slug in used_slugs:
            n = 2
            while f"{slug}-{n}" in used_slugs:
                n += 1
            slug = f"{slug}-{n}"
            movie["slug"] = slug
        used_slugs.add(slug)
        dm = district_meta_from_rows(mrows)
        # District supplies the card thumbnail; BookMyShow's xxlarge image (when a
        # BMS row carries its image id) supplies the background. Both are mirrored
        # to /assets/posters/<slug>-{thumb,bg}.jpg in the dashboard repo; if a
        # download fails we keep the remote URL so nothing breaks.
        movie["poster"] = dm["poster"] or _global_district_poster(title)
        movie["poster"] = mirror_posters(slug, movie["poster"], _bms_image_from_rows(
                    mrows, [c for c in [_bms_codes.get(canon_key(title))] if c], title=title))
        movie["meta"] = dm["meta"]
        movie["last_updated"] = last_updated
        movies_for_history[movie["slug"]] = movie
        # FULL tree (admin)
        with open(os.path.join(m_dir, movie["slug"] + ".json"), "w", encoding="utf-8") as f:
            json.dump(movie, f, ensure_ascii=False, separators=(",", ":"))
        # PUBLIC tree (trimmed) — no theatre/area/showtime
        k = movie["kpi"]
        index.append({
            "slug": movie["slug"], "title": title,
            "sources": sorted({r.get("source") for r in mrows if r.get("source")}),
            "languages": movie["languages"], "formats": movie["formats"],
            "poster": ({"thumb": movie["poster"]["thumb"]} if movie["poster"] else None),
            "gross": k["gross"], "sold": k["sold"], "occupancy": k["occupancy"],
            "totalSeats": k["seats"],
            "shows": k["shows"], "theatres": k["theatres"], "cities": k["cities"],
            "states": k["states"], "housefull": k["housefull"], "fastfilling": k["fastfilling"],
            "genres": (movie["meta"]["genres"] if movie.get("meta") else []),
            "certification": (movie["meta"]["certification"] if movie.get("meta") else None),
            "runTime": (movie["meta"]["runTime"] if movie.get("meta") else None),
            "eventCode": (movie["meta"]["eventCode"] if movie.get("meta") else None),
        })
    index.sort(key=lambda x: x["gross"], reverse=True)

    national = blank()
    for it in index:
        national["gross"] += it["gross"]
        national["sold"] += it["sold"]
        national["shows"] += it["shows"]
    national["gross"] = round(national["gross"], 2)

    national_obj = {
        "mode": mode, "date": date, "last_updated": last_updated,
        "totals": {
            "movies": len(index), "gross": national["gross"],
            "sold": national["sold"], "shows": national["shows"],
        },
        "movies": index,
    }
    # national index is aggregate-only -> identical for both trees
    with open(os.path.join(out_dir, "national.json"), "w", encoding="utf-8") as f:
        json.dump(national_obj, f, ensure_ascii=False, separators=(",", ":"))

    note = f", {skipped} not tracked" if skipped else ""
    print(f"    + {mode}/{date}: {len(index)} movies, {len(rows)} shows{note}")
    if skipped and not index:
        print(f"    ! {mode}/{date}: tracked list matched NOTHING here - "
              f"check the titles in {TRACK_FILE}")

    # Copy the multiplex report (selected-chain breakdown) through as-is, if
    # multiplex_report.py has produced one for this date. DAILY only — advance
    # is forward-looking pre-sales, not real box-office collections, and the
    # multiplex report is about actual per-theatre gross/shows tracked so far.
    # It's aggregate-only, so identical for both the FULL and PUBLIC trees,
    # same as national.json.
    multiplex_src = os.path.join(src_dir, "multiplex.json")
    if mode == "daily" and os.path.exists(multiplex_src):
        with open(multiplex_src, encoding="utf-8") as f:
            multiplex_obj = json.load(f)
        with open(os.path.join(out_dir, "multiplex.json"), "w", encoding="utf-8") as f:
            json.dump(multiplex_obj, f, ensure_ascii=False, separators=(",", ":"))

    # Copy the all-India territory report through as-is, if territory_report.py
    # has produced one for this date. Generated for BOTH daily and advance —
    # unlike the multiplex report, advance territory data is exactly what lets
    # a tracked upcoming release's opening-day advance be seen territory-wise
    # (e.g. The Paradise's 20260923 premiere advance). Aggregate-only, so
    # identical for both the FULL and PUBLIC trees.
    territory_src = os.path.join(src_dir, "territory.json")
    if os.path.exists(territory_src):
        with open(territory_src, encoding="utf-8") as f:
            territory_obj = json.load(f)
        with open(os.path.join(out_dir, "territory.json"), "w", encoding="utf-8") as f:
            json.dump(territory_obj, f, ensure_ascii=False, separators=(",", ":"))

    # ...and its tracked-only companion (only the highlighted/dashboard
    # titles) — same daily+advance availability as above.
    territory_tracked_src = os.path.join(src_dir, "territory_tracked.json")
    if os.path.exists(territory_tracked_src):
        with open(territory_tracked_src, encoding="utf-8") as f:
            territory_tracked_obj = json.load(f)
        with open(os.path.join(out_dir, "territory_tracked.json"), "w", encoding="utf-8") as f:
            json.dump(territory_tracked_obj, f, ensure_ascii=False, separators=(",", ":"))

    return {"date": date, "last_updated": last_updated, "movies": movies_for_history}


def build_history(mode, per_date, out_dir):
    """Combine all dates of a mode into per-movie history (day/city/state/format wise)."""
    h_dir = os.path.join(out_dir, "history")
    os.makedirs(h_dir, exist_ok=True)
    by_slug = defaultdict(list)            # slug -> [(date, movie)]
    for d in sorted(per_date, key=lambda x: x["date"]):
        for slug, movie in d["movies"].items():
            by_slug[slug].append((d["date"], movie))

    # A day only counts toward the tracked TOTAL once it has actually ended.
    # "Today" is still filling up, so it stays complete=False and is excluded
    # from the totals (the UI shows it as a live row).
    _ist = _dt.timezone(_dt.timedelta(hours=5, minutes=30))
    today_ymd = _dt.datetime.now(_ist).strftime("%Y%m%d")

    for slug, entries in by_slug.items():
        days = []
        prev = None
        for i, (date, movie) in enumerate(entries, 1):
            k = movie["kpi"]
            row = {"day": i, "date": date,
                   "complete": str(date).replace("-", "") < today_ymd,
                   "gross": k["gross"], "sold": k["sold"],
                   "seats": k.get("seats", 0), "shows": k["shows"],
                   "theatres": k.get("theatres", 0), "cities": k.get("cities", 0),
                   "housefull": k.get("housefull", 0),
                   "fastfilling": k.get("fastfilling", 0),
                   "occupancy": k["occupancy"]}
            # day-over-day comparison vs the previous tracked day
            if prev:
                row["grossChange"] = round(k["gross"] - prev["gross"], 2)
                row["soldChange"] = k["sold"] - prev["sold"]
                row["showsChange"] = k["shows"] - prev["shows"]
                row["occupancyChange"] = round(k["occupancy"] - prev["occupancy"], 2)
                row["grossChangePct"] = (
                    round((k["gross"] - prev["gross"]) / prev["gross"] * 100, 1)
                    if prev["gross"] else None
                )
                row["soldChangePct"] = (
                    round((k["sold"] - prev["sold"]) / prev["sold"] * 100, 1)
                    if prev["sold"] else None
                )
            else:
                row["grossChange"] = row["soldChange"] = row["showsChange"] = None
                row["occupancyChange"] = row["grossChangePct"] = row["soldChangePct"] = None
            days.append(row)
            prev = k
        # running total across tracked days (cumulative box office)
        run = 0.0
        for d in days:
            run += d["gross"]
            d["cumulativeGross"] = round(run, 2)
        latest = entries[-1][1]

        # ---- tracked totals across CLOSED days ----
        done = [d for d in days if d["complete"]]
        tot = {
            "days": len(done),
            "gross": round(sum(d["gross"] for d in done), 2),
            "sold": sum(d["sold"] for d in done),
            "seats": sum(d["seats"] for d in done),
            "shows": sum(d["shows"] for d in done),
            "housefull": sum(d["housefull"] for d in done),
            "fastfilling": sum(d["fastfilling"] for d in done),
            # theatres/cities repeat every day -> peak footprint, never a sum
            "theatres": max([d["theatres"] for d in done], default=0),
            "cities": max([d["cities"] for d in done], default=0),
            "bestDay": max(done, key=lambda d: d["gross"])["day"] if done else None,
        }
        tot["occupancy"] = occ(tot["sold"], tot["seats"])

        # ---- city / state / format: accumulate across closed days ----
        # These used to be a copy of the LATEST day's snapshot, which made a
        # "historical" table really a "today so far" table. If no day has closed
        # yet, fall back to the live snapshot and flag it (cumulative=False).
        done_entries = [e for e in entries
                        if str(e[0]).replace("-", "") < today_ymd]
        src_entries = done_entries or entries
        cumulative = bool(done_entries)

        SUM = ("gross", "sold", "seats", "shows", "housefull", "fastfilling")

        def _acc(dst, src):
            for key in SUM:
                dst[key] = dst.get(key, 0) + (src.get(key) or 0)

        st_acc, ct_acc, fm_acc = {}, {}, {}
        for _date, mv in src_entries:
            for st in mv.get("states", []):
                a = st_acc.setdefault(st["state"],
                                      {"state": st["state"], "theatres": 0, "cities": 0})
                _acc(a, st)
                a["theatres"] = max(a["theatres"], st.get("theatres") or 0)
                a["cities"] = max(a["cities"], st.get("cities") or 0)
                for c in st.get("cityList", []):
                    b = ct_acc.setdefault((st["state"], c["city"]),
                                          {"city": c["city"], "state": st["state"],
                                           "theatres": 0})
                    _acc(b, c)
                    b["theatres"] = max(b["theatres"], c.get("theatres") or 0)
            for f in mv.get("formatSummary", []):
                g = fm_acc.setdefault(f["format"], {"format": f["format"]})
                _acc(g, f)

        for rec in list(st_acc.values()) + list(ct_acc.values()) + list(fm_acc.values()):
            rec["gross"] = round(rec.get("gross", 0), 2)
            rec["occupancy"] = occ(rec.get("sold", 0), rec.get("seats", 0))

        by_gross = lambda x: x["gross"]
        states = sorted(st_acc.values(), key=by_gross, reverse=True)
        cities = sorted(ct_acc.values(), key=by_gross, reverse=True)
        formats = sorted(fm_acc.values(), key=by_gross, reverse=True)

        hist_obj = {"title": latest["title"], "last_updated": latest.get("last_updated"),
                    "cumulative": cumulative, "daysCounted": len(done_entries),
                    "totals": tot, "days": days, "cities": cities[:50],
                    "states": states, "formats": formats}
        with open(os.path.join(h_dir, slug + ".json"), "w", encoding="utf-8") as f:
            json.dump(hist_obj, f, ensure_ascii=False, separators=(",", ":"))


def _slugify_post(s):
    s = (s or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "post"


def _parse_frontmatter(text):
    """Minimal YAML-frontmatter parser for Decap-generated markdown.

    Handles:
      ---
      key: value
      key: "quoted value"
      rating: 4
      ---
      body text...
    Returns (fields_dict, body_str). No external yaml dependency.
    """
    text = text.replace("\r\n", "\n")
    fields, body = {}, text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            fm = text[3:end].strip("\n")
            body = text[end + 4 :].lstrip("\n")
            for line in fm.split("\n"):
                if not line.strip() or ":" not in line:
                    continue
                key, val = line.split(":", 1)
                key, val = key.strip(), val.strip()
                # strip surrounding quotes
                if (val.startswith('"') and val.endswith('"')) or (
                    val.startswith("'") and val.endswith("'")
                ):
                    val = val[1:-1]
                # numbers
                if re.fullmatch(r"-?\d+", val):
                    val = int(val)
                elif re.fullmatch(r"-?\d+\.\d+", val):
                    val = float(val)
                fields[key] = val
    return fields, body.strip()


def _read_posts(folder):
    """Read every .md file in folder -> list of {fields..., slug, body}."""
    posts = []
    if not os.path.isdir(folder):
        return posts
    for name in sorted(os.listdir(folder)):
        if not name.lower().endswith(".md"):
            continue
        path = os.path.join(folder, name)
        try:
            with open(path, "r", encoding="utf-8") as f:
                fields, body = _parse_frontmatter(f.read())
        except Exception as e:
            print(f"  [editorial] skip {name}: {e}")
            continue
        # slug: explicit field, else from title/movie, else filename
        slug = fields.get("slug") or _slugify_post(
            str(fields.get("title") or fields.get("movie") or os.path.splitext(name)[0])
        )
        post = dict(fields)
        post["slug"] = slug
        post["body"] = fields.get("body") or body
        posts.append(post)
    # newest first by date
    posts.sort(key=lambda p: str(p.get("date", "")), reverse=True)
    return posts


def build_editorial():
    """Turn content/{news,reviews,boxoffice}/*.md into data/{...}.json.

    Content lives in the repo under content/ (survives the data/ rebuild).
    Safe no-op if the folders don't exist yet.
    """
    content_root = os.path.join(HERE, "content")
    sections = {
        "news": ("news", ["slug", "title", "date", "image", "summary", "body"]),
        "reviews": ("reviews", ["slug", "movie", "rating", "date", "poster", "summary", "body"]),
        "boxoffice": ("boxoffice", ["slug", "title", "movie", "reportType", "date", "image", "body"]),
    }
    for out_name, (folder, keep) in sections.items():
        posts = _read_posts(os.path.join(content_root, folder))
        cleaned = []
        for p in posts:
            cleaned.append({k: p[k] for k in keep if k in p and p[k] != ""})
        for root in (OUT,):
            with open(os.path.join(root, f"{out_name}.json"), "w", encoding="utf-8") as f:
                json.dump(cleaned, f, ensure_ascii=False, separators=(",", ":"))
        print(f"  editorial/{out_name}: {len(cleaned)} post(s)")


def main(collector):
    for d in (OUT,):
        if os.path.isdir(d):
            shutil.rmtree(d)
        os.makedirs(d, exist_ok=True)

    manifest = {"generated": "", "timezone": "Asia/Kolkata", "modes": {}}
    import datetime
    manifest["generated"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    all_titles = {}                                      # title -> has_poster
    history_source = {}                                  # mode -> per_date (for history)
    slug_dates = {"daily": {}, "advance": {}}            # mode -> slug -> [dates]
    for mode, meta in MODES.items():
        data_root = os.path.join(collector, mode, "data")
        dates = []
        per_date = []
        if os.path.isdir(data_root):
            for date in sorted(os.listdir(data_root)):
                src = os.path.join(data_root, date)
                if not os.path.isdir(src) or not re.fullmatch(r"\d{8}", date):
                    continue
                out_dir = os.path.join(OUT, mode, date)
                os.makedirs(out_dir, exist_ok=True)
                res = build_date(mode, date, src, out_dir)
                if res:
                    dates.append(date)
                    per_date.append(res)
                    for slug, mv in res["movies"].items():
                        all_titles[mv["title"]] = bool(mv.get("poster"))
                        slug_dates.setdefault(mode, {}).setdefault(slug, []).append(date)
        if per_date:
            build_history(mode, per_date, os.path.join(OUT, mode))
            history_source[mode] = per_date
        manifest["modes"][mode] = {
            "label": meta["label"], "runsPerDay": meta["runsPerDay"],
            "runTimes": meta["runTimes"], "dates": dates,
        }
        print(f"  {mode}: {len(dates)} date(s)")

    # ---- upcoming releases -------------------------------------------------
    # District's showtime API carries NO release date (its payload has name,
    # genres, censor, isNew... and nothing release-ish), so we infer:
    #
    #     has advance bookings + has NEVER appeared in daily => not released yet
    #     -> its OPENING DAY is the earliest advance date it appears on
    #
    # A running film is in daily every day, so it can never be flagged. This is
    # what lets the dashboard show "Opening Day Advance" instead of a row of
    # meaningless advance date chips.
    released = set(slug_dates.get("daily", {}))
    upcoming = {}
    for slug, dts in slug_dates.get("advance", {}).items():
        if slug in released or not dts:
            continue
        upcoming[slug] = min(dts)                        # opening day (YYYYMMDD)

    manifest["upcoming"] = upcoming
    # Per-movie date lists. modes.<mode>.dates is the GLOBAL set of dates in
    # a tree; it does NOT mean every movie has data on every date. The
    # dashboard needs which dates belong to EACH film, or a movie shows date
    # chips (another film's release day) that have no data for it.
    manifest["movieDates"] = {
        mode: {slug: sorted(set(dts)) for slug, dts in by_slug.items()}
        for mode, by_slug in slug_dates.items()
    }
    if upcoming:
        print(f"  upcoming: {len(upcoming)} unreleased film(s)")
        for slug, d in sorted(upcoming.items(), key=lambda x: x[1]):
            print(f"    {d}  {slug}")

    # stamp it onto the movie files so the dashboard doesn't need the manifest
    for slug, open_day in upcoming.items():
        iso = f"{open_day[:4]}-{open_day[4:6]}-{open_day[6:8]}"
        for date in slug_dates["advance"][slug]:
            fp = os.path.join(OUT, "advance", date, "m", slug + ".json")
            if not os.path.exists(fp):
                continue
            try:
                with open(fp, encoding="utf-8") as f:
                    mj = json.load(f)
                # NB: "meta" is often present but NULL (District gives no meta
                # for these films). setdefault() then returns None and the next
                # assignment blows up -> "'NoneType' does not support item
                # assignment". Coerce to a dict instead of assuming.
                if not isinstance(mj.get("meta"), dict):
                    mj["meta"] = {}
                mj["meta"]["upcoming"] = True
                mj["meta"]["openingDay"] = open_day
                # releaseDate is NOT guessed here (see district_meta_from_rows
                # and the user-provided block just below) -- meta.upcoming /
                # meta.openingDay alone still let the dashboard show "Opening
                # Day Advance" for a still-unreleased film with no manually
                # set release date yet.
                with open(fp, "w", encoding="utf-8") as f:
                    json.dump(mj, f, ensure_ascii=False, separators=(",", ":"))
            except Exception as e:
                print(f"    ! could not flag {slug} ({e})")

    # ---- user-provided release date, from tracked_movies.json --------------
    # This is now the ONLY source meta.releaseDate is ever set from. District's
    # own release-date field is never used (district_meta_from_rows always
    # leaves it None) and there is no "infer it from first daily date"
    # fallback either -- both were unreliable/wrong often enough that the
    # user now maintains release dates by hand instead. A movie the user
    # hasn't set a date for simply has no releaseDate (Premiere/Day-N
    # labelling falls back to the upcoming/openingDay signal above, or shows
    # nothing, rather than a guess).
    # Takes priority over BOTH District's own field and the "first daily
    # date" backfill just above: it's a fact the user is stating directly,
    # not a guess, so it OVERWRITES whatever is already there (including a
    # wrong District value) rather than only filling gaps. This is the
    # reliable way to get Premiere/Day-N labelling right for any movie,
    # including ones build_data can't infer a release day for at all yet
    # (still advance-only, no daily data to infer from).
    user_release_dates = load_tracked_release_dates()
    if user_release_dates:
        all_slugs = set()
        for by_slug in slug_dates.values():
            all_slugs.update(by_slug.keys())
        touched = 0
        for slug in all_slugs:
            iso = user_release_dates.get(slug)
            if not iso:
                continue
            for mode in ("advance", "daily"):
                for date in slug_dates.get(mode, {}).get(slug, []):
                    fp = os.path.join(OUT, mode, date, "m", slug + ".json")
                    if not os.path.exists(fp):
                        continue
                    try:
                        with open(fp, encoding="utf-8") as f:
                            mj = json.load(f)
                        if not isinstance(mj.get("meta"), dict):
                            mj["meta"] = {}
                        if mj["meta"].get("releaseDate") == iso:
                            continue
                        mj["meta"]["releaseDate"] = iso
                        with open(fp, "w", encoding="utf-8") as f:
                            json.dump(mj, f, ensure_ascii=False, separators=(",", ":"))
                        touched += 1
                    except Exception as e:
                        print(f"    ! could not set user releaseDate for {slug} ({e})")
        if touched:
            print(f"  releaseDate: {touched} file(s) set from tracked_movies.json")

    # The "Historical" tab must always show what we ACTUALLY tracked day by day
    # (i.e. DAILY actuals), never the advance/pre-sales snapshot. Advance numbers
    # are a forward-looking booking state, not a day's real box office, so a
    # day-over-day comparison across them is meaningless.
    # So: rebuild history from the DAILY dates and write it into every mode
    # folder, which makes data/<mode>/history/<slug>.json identical and correct
    # whichever tab the dashboard is on.
    daily_per_date = history_source.get("daily")
    if daily_per_date:
        for mode in MODES:
            build_history("daily", daily_per_date, os.path.join(OUT, mode))
        print(f"  history: built from DAILY actuals ({len(daily_per_date)} day(s)), "
              f"with day-over-day comparison")
    else:
        print("  history: no daily data yet - skipped")

    with open(os.path.join(OUT, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    prefetch_tracked_posters(collector, {s for sl in slug_dates.values() for s in sl})

    # editorial content (admin-posted news / reviews / box office) -> both trees
    build_editorial()

    print("done ->", OUT)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python3 build_data.py /path/to/datacollector")
        sys.exit(1)
    main(os.path.abspath(sys.argv[1]))