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

MODES = {
    "advance": {"label": "Advance", "runsPerDay": 6,
                "runTimes": ["08:45", "11:45", "14:45", "17:45", "20:45", "23:30"]},
    "daily":   {"label": "Daily", "runsPerDay": 13,
                "runTimes": ["03:00", "05:00", "07:00", "08:00", "10:00", "11:00",
                             "13:00", "14:00", "16:00", "17:00", "19:00", "20:00", "22:00"]},
}

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "data")              # FULL tree (admin)

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
    """Derive {poster, meta} from the District worker's movieInfo embedded in rows."""
    best = None
    fallback = None
    release_date = None
    cast = []
    for r in rows:
        mi = r.get("movieInfo")
        if not mi:
            continue
        # cast / release date may live on a different row than the poster —
        # keep the first non-empty value we find across all rows.
        if release_date is None:
            release_date = _extract_release_date(mi)
        if not cast:
            cast = _extract_cast(mi)
        if fallback is None and (mi.get("poster") or mi.get("genres") or mi.get("censor")
                                 or mi.get("duration") or mi.get("trailer")):
            fallback = mi
        if mi.get("poster"):
            best = mi
            if release_date and cast:
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
        "releaseDate": release_date,
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
#  Per-movie aggregation
# --------------------------------------------------------------------------- #
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

        state = (r.get("state") or "Unknown").strip() or "Unknown"
        city = (r.get("city") or "Unknown").strip() or "Unknown"
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

    grouped = defaultdict(list)
    display = {}                       # canon key -> clean display title
    for r in rows:
        base, _, _ = parse_key(r.get("movie", ""))
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

    index = []
    movies_for_history = {}
    used_slugs = set()
    for ck, mrows in grouped.items():
        title = display.get(ck) or ck
        movie = build_movie(title, mrows)
        slug = movie["slug"]
        if slug in used_slugs:
            n = 2
            while f"{slug}-{n}" in used_slugs:
                n += 1
            slug = f"{slug}-{n}"
            movie["slug"] = slug
        used_slugs.add(slug)
        dm = district_meta_from_rows(mrows)
        # Posters come straight from District — the raw CDN thumb/cover URLs.
        # No local mirror: the dashboard loads them directly, and they refresh
        # automatically whenever District updates the artwork.
        movie["poster"] = dm["poster"] or _global_district_poster(title)
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

    print(f"    + {mode}/{date}: {len(index)} movies, {len(rows)} shows")
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
                # only fill releaseDate if District never gave us one
                if not mj["meta"].get("releaseDate"):
                    mj["meta"]["releaseDate"] = iso
                with open(fp, "w", encoding="utf-8") as f:
                    json.dump(mj, f, ensure_ascii=False, separators=(",", ":"))
            except Exception as e:
                print(f"    ! could not flag {slug} ({e})")

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

    # editorial content (admin-posted news / reviews / box office) -> both trees
    build_editorial()

    print("done ->", OUT)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python3 build_data.py /path/to/datacollector")
        sys.exit(1)
    main(os.path.abspath(sys.argv[1]))