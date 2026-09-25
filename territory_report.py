#!/usr/bin/env python3
"""
Territory Report builder.
==========================
Builds an "All India" box-office report broken down by film-distribution
TERRITORY (Nizam, Ceded, UA - Uttar Andhra, East, West, Krishna, Guntur, Nellore, Kerala,
Bengaluru City / rest of Karnataka, Chennai City / rest of Tamil Nadu), the way
the Telugu/South film trade reports collections, straight from the collector's
raw show-level rows.

DAILY ONLY. Same reasoning as multiplex_report.py: advance is forward-looking
pre-sales, not actual collections, so it isn't included here.

Reads (in order of preference, first one found wins):
    daily/data/<date>/finaldetailed.json   {"last_updated":..., "data":[rows]}
    daily/data/<date>/detailed*.json       raw per-shard rows (all 9 shards)

Each row (see scraper/parser.py) looks like:
    {"movie":..., "venue":..., "chain":..., "city":..., "state":...,
     "grossRevenue":..., "ticketsSold":..., "totalSeats":..., "time":..., "date":...}
(District/daily rows may instead use "gross"/"sold" — both schemas are
handled tolerantly, mirroring build_data.py's row_vals()). "totalSeats" is
what occupancy (sold/seats %) is computed from at every level of the report.

A row is placed into a territory by matching its "city" against the
territory/*.json hierarchies (state -> district -> [taluk ->] area/village),
scoped first by the row's "state" field (see territory_config.json). Rows
whose state isn't one of the configured groups fall into "Rest of India"
(broken down by state); rows whose state IS a configured group but whose city
can't be matched anywhere fall into that group's "Other (...)" bucket, so
nothing is ever silently dropped from the totals.

territory_config.json (root of the collector) lists the state->file mapping
and canonical territory display order, so it can be edited without touching
this script.

Writes:
    daily/data/<date>/territory.json          all movies (raw, admin view)
    daily/data/<date>/territory_tracked.json  only the currently tracked/
                                               highlighted titles from
                                               tracked_movies.json — same
                                               filtering rules build_data.py
                                               uses to decide what's actually
                                               shown on the public dashboard
                                               (mode/hide_untracked honored)

Usage:
    python territory_report.py --date 20260920
    python territory_report.py                 # latest date
"""
import argparse
import difflib
import json
import os
import re
import sys

from multiplex_report import _load_rows, _latest_date

HERE = os.path.dirname(os.path.abspath(__file__))
TERRITORY_DIR = os.path.join(HERE, "territory")
CONFIG_FILE = os.path.join(HERE, "territory_config.json")

# Common alternate/English spellings seen in the "city" field that don't
# literally appear in the territory/*.json hierarchy under that spelling.
CITY_ALIASES = {
    "vizag": "visakhapatnam",
    "trivandrum": "thiruvananthapuram",
    "calicut": "kozhikode",
    "cochin": "kochi",
    "trichy": "tiruchirappalli",
    "pondicherry": "puducherry",
    "bangalore": "bengaluru",
    "rajahmundry": "rajamahendravaram",
    "tuticorin": "thoothukudi",
    "ooty": "udhagamandalam",
}


def _row_vals(r):
    """Read seats/sold/gross tolerant of both collector schemas (mirrors
    build_data.py's row_vals): advance/combined rows use
    ticketsSold/grossRevenue; District/daily rows use sold/gross."""
    seats = r.get("totalSeats") or 0
    sold = r.get("ticketsSold")
    if sold is None:
        sold = r.get("sold")
    gross = r.get("grossRevenue")
    if gross is None:
        gross = r.get("gross")
    return int(seats or 0), int(sold or 0), float(gross or 0.0)


def _occ(sold, seats):
    return round(sold / seats * 100, 2) if seats else 0.0


def _norm(s):
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


def _slug(s):
    return re.sub(r"[^a-z0-9]+", "-", _norm(s)).strip("-") or "unknown"


def _candidates(city):
    """City field -> normalized name candidates to try, unpacking
    'Vizag (Visakhapatnam)' / 'Mysuru (Mysore)' style parenthetical alt-names
    and applying CITY_ALIASES."""
    base = _norm(city)
    if not base:
        return []
    cands = [base]
    m = re.match(r"^(.*?)\s*\((.*?)\)\s*$", base)
    if m:
        cands.append(_norm(m.group(1)))
        cands.append(_norm(m.group(2)))
    out = []
    for c in cands:
        if c and c not in out:
            out.append(c)
        alias = CITY_ALIASES.get(c)
        if alias and alias not in out:
            out.append(alias)
    return out


def load_config():
    with open(CONFIG_FILE, encoding="utf-8") as f:
        return json.load(f)


def _index_file(path):
    """Build a normalized-name -> top-level-territory index for one
    territory/*.json file, by walking every dict key and every list string at
    any depth under each top-level bucket."""
    with open(path, encoding="utf-8") as f:
        tree = json.load(f)
    index = {}

    def walk(node, bucket):
        if isinstance(node, dict):
            for k, v in node.items():
                index.setdefault(_norm(k), bucket)
                walk(v, bucket)
        elif isinstance(node, list):
            for item in node:
                if isinstance(item, str):
                    index.setdefault(_norm(item), bucket)

    for bucket, subtree in tree.items():
        walk(subtree, bucket)
    return index


def _apply_label_overrides(index, overrides):
    """Rename territory bucket labels after the fact (e.g. "Vizag" ->
    "UA - Uttar Andhra") without touching the underlying territory/*.json
    hierarchy files, which encode city->territory *classification*, not
    display naming. Overrides are matched case-insensitively against the
    bucket label itself."""
    if not overrides:
        return index
    lut = {k.casefold(): v for k, v in overrides.items()}
    return {city: lut.get(bucket.casefold(), bucket) for city, bucket in index.items()}


def build_groups(cfg):
    overrides = cfg.get("label_overrides", {})
    groups = []
    for entry in cfg["state_files"]:
        path = os.path.join(TERRITORY_DIR, entry["file"])
        groups.append({
            "match": [m.lower() for m in entry.get("match", [])],
            "index": _apply_label_overrides(_index_file(path), overrides),
            "file": entry["file"],
            "unmatched_label": entry.get("unmatched_label", f"Other ({entry['file']})"),
        })
    return groups


def _match_group(state, groups):
    state_norm = _norm(state)
    for g in groups:
        if any(m in state_norm for m in g["match"]):
            return g
    return None


def resolve_bucket(row, groups, rest_key, rest_label):
    """Return (key, label, is_rest_of_india) for a row."""
    group = _match_group(row.get("state"), groups)
    if not group:
        return rest_key, rest_label, True

    cands = _candidates(row.get("city"))
    for cand in cands:
        bucket = group["index"].get(cand)
        if bucket:
            return _slug(bucket), bucket, False

    # rare fallback: substring match against every indexed name
    for cand in cands:
        if not cand:
            continue
        for name, bucket in group["index"].items():
            if cand in name or name in cand:
                return _slug(bucket), bucket, False

    # last resort: fuzzy match for spelling variants not caught above
    # (e.g. "Rajahmundry" vs "Rajamahendravaram", "Mahbubnagar" vs
    # "Mahabubnagar", "Tirupur" vs "Tiruppur").
    all_names = list(group["index"].keys())
    for cand in cands:
        if not cand:
            continue
        close = difflib.get_close_matches(cand, all_names, n=1, cutoff=0.82)
        if close:
            return _slug(group["index"][close[0]]), group["index"][close[0]], False

    other_key = f"other-{_slug(group['file'].rsplit('.', 1)[0])}"
    return other_key, group["unmatched_label"], False


# ---------------------------------------------------------------------------
# Tracked-title filtering (mirrors build_data.py's dashboard-visibility rules,
# so "tracked only" here means exactly the titles currently shown on the
# site — tracked_movies.json's "movies" list, honoring mode/hide_untracked).
# ---------------------------------------------------------------------------
TRACK_FILE = os.path.join(HERE, "tracked_movies.json")
_TITLE_TAG_RE = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]\s*$")


def _canon_title(t):
    """'Lenin (2D - Telugu)' -> 'lenin'"""
    t = (t or "").strip()
    prev = None
    while prev != t and t:
        prev = t
        t = _TITLE_TAG_RE.sub("", t).strip()
    return t.casefold()


def _title_slug(t):
    return re.sub(r"[^a-z0-9]+", "-", (t or "").lower()).strip("-")


def load_tracked_titles():
    """Return (mode, keyset). mode 'all' -> no filtering (nothing is hidden)."""
    if not os.path.exists(TRACK_FILE):
        return ("all", set())
    try:
        with open(TRACK_FILE, encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception as e:
        print(f"! tracked_movies.json unreadable ({e}) -> tracked report = all movies")
        return ("all", set())

    mode = str(cfg.get("mode", "selected")).strip().lower()
    if mode not in ("all", "selected"):
        mode = "selected"

    if mode == "selected" and cfg.get("hide_untracked") is False:
        return ("all", set())

    keys = set()
    for entry in cfg.get("movies") or []:
        if not isinstance(entry, str) or not entry.strip():
            continue
        raw = entry.strip()
        keys.add(_canon_title(raw))
        keys.add(_title_slug(raw))
        keys.add(_canon_title(raw.replace("-", " ")))

    if mode == "selected" and not keys:
        return ("all", set())

    return (mode, keys)


def _is_tracked(title, tracked):
    mode, keys = tracked
    if mode == "all":
        return True
    if not title:
        return False
    c = _canon_title(title)
    s = _title_slug(title)
    return c in keys or s in keys or _canon_title(s.replace("-", " ")) in keys


# ---------------------------------------------------------------------------
# Group roll-ups (Nizam, APTG Total, Karnataka Total, Tamil Nadu Total,
# Kerala Total, ROI Total, ...) -- sums of specific territory rows, purely
# for easy dashboard consumption. Defined by territory LABEL (not slug) so
# they are readable/editable. Override or extend via a "groups" array in
# territory_config.json, e.g.:
#   "groups": [
#     {"label": "Nizam", "members": ["Hyderabad", "Nizam Districts"], "children": ["Hyderabad"]},
#     {"label": "APTG Total", "members": ["Nizam", "Vizag", "Ceded", "East",
#                                          "Guntur", "Krishna", "West", "Nellore"]}
#   ]
# A member can be a raw territory name OR the label of a group defined
# earlier in the same list (so "APTG Total" can sum "Nizam" as one lump
# alongside the still-standalone AP districts) -- groups are processed in
# order and each one is registered for later groups to reference as soon as
# it is built. "children" lists members (raw territories only) that are
# already included in this group's sum but should also be shown nested
# underneath it for transparency (e.g. Hyderabad under Nizam) -- they add
# nothing extra to the total, they are a display-only breakdown of a member
# that is already counted.
# Any group whose members aren't found (yet) is silently skipped (e.g.
# Kerala/Tamil Nadu when a title has no business there), so it's safe to
# keep every group defined here for every run.
# ---------------------------------------------------------------------------
DEFAULT_GROUPS = [
    {
        "label": "Nizam", "members": ["Hyderabad", "Nizam Districts"], "children": ["Hyderabad"],
        "note": "Territory splits are calculated for Andhra Pradesh and Telangana only. "
                "Sub-regions such as Hyderabad are already counted inside their parent territory.",
    },
    {"label": "APTG Total", "members": ["Nizam", "UA - Uttar Andhra", "Ceded", "East", "Guntur",
                                         "Krishna", "West", "Nellore"]},
    {"label": "Karnataka Total", "members": ["Bengaluru City", "Karnataka"]},
    {"label": "Tamil Nadu Total", "members": ["Chennai City", "Tamil Nadu"]},
    {"label": "Kerala Total", "members": ["Kerala"]},
    {"label": "ROI Total", "members": ["Rest of India"]},
]


def _compute_groups(territories, stats_by_key, group_defs, rest_key, rest_label):
    """Roll up territories (and, chained, other already-built groups) into
    named groups (Nizam, APTG Total, Karnataka Total, ...) using the exact
    sold/seats each territory was built from, so occupancy is a true
    weighted average rather than an average-of-percentages.

    group_defs is processed in order; each group's "members" may name a raw
    territory (by its display label) or the label of an earlier group in the
    same list, so a grand total like "APTG Total" can sum "Nizam" (itself
    already Hyderabad + Nizam Districts) alongside the still-standalone AP
    districts. A group's optional "children" lists members (raw territories)
    that are already inside its sum but should also be surfaced nested
    underneath it for display -- e.g. Hyderabad shown under Nizam -- without
    adding anything extra to the total.

    Territories never referenced by any group_defs entry are left standing
    on their own in "territories" (e.g. an "Other (...)" fallback bucket)."""
    pool = {}
    for t in territories:
        st = stats_by_key.get(t["key"], {"sold": 0, "seats": 0})
        pool[t["key"]] = {
            "label": t["label"], "gross": t["gross"], "shows": t["shows"],
            "sold": st["sold"], "seats": st["seats"],
        }

    out = []
    claimed = set()
    for g in group_defs:
        member_keys = [_slug(m) for m in g["members"]]
        present = [k for k in member_keys if k in pool]
        if not present:
            continue
        sold = sum(pool[k]["sold"] for k in present)
        seats = sum(pool[k]["seats"] for k in present)
        gross = round(sum(pool[k]["gross"] for k in present), 2)
        shows = sum(pool[k]["shows"] for k in present)
        key = _slug(g["label"])
        entry = {
            "key": key,
            "label": g["label"],
            "gross": gross,
            "shows": shows,
            "sold": sold,
            "occupancy": _occ(sold, seats),
            "members": present,
        }
        if g.get("children"):
            entry["children"] = [
                {
                    "key": ck, "label": pool[ck]["label"],
                    "gross": pool[ck]["gross"], "shows": pool[ck]["shows"],
                    "sold": pool[ck]["sold"], "occupancy": _occ(pool[ck]["sold"], pool[ck]["seats"]),
                }
                for ck in (_slug(c) for c in g["children"]) if ck in pool
            ]
        if g.get("note"):
            entry["note"] = g["note"]
        out.append(entry)
        claimed.update(present)
        # Register the group itself so a later group (e.g. "APTG Total") can
        # reference it by label as one of its own members.
        pool[key] = {"label": g["label"], "gross": gross, "shows": shows, "sold": sold, "seats": seats}

    # Rest of India is already a single rolled-up bucket on its own (with a
    # per-state breakdown) -- mirror it into "groups" too so a dashboard can
    # treat every entry in "groups" uniformly, even if a custom config
    # forgets to define an ROI group explicitly.
    if rest_key in pool and rest_key not in claimed and rest_key not in {o["key"] for o in out}:
        p = pool[rest_key]
        out.append({
            "key": rest_key,
            "label": rest_label,
            "gross": p["gross"],
            "shows": p["shows"],
            "sold": p["sold"],
            "occupancy": _occ(p["sold"], p["seats"]),
            "members": [rest_key],
        })

    return out


def _aggregate(rows, groups, rest_key, rest_label, order):
    """Bucket rows into territories and return (territories, total_gross, total_shows)."""
    buckets = {}
    for r in rows:
        key, label, is_rest = resolve_bucket(r, groups, rest_key, rest_label)
        b = buckets.setdefault(key, {"label": label, "movies": {}, "states": {}})
        movie = r.get("movie", "Unknown")
        seats, sold, gross = _row_vals(r)
        acc = b["movies"].setdefault(movie, {"gross": 0.0, "shows": 0, "sold": 0, "seats": 0})
        acc["gross"] += gross
        acc["shows"] += 1
        acc["sold"] += sold
        acc["seats"] += seats
        if is_rest:
            state = (r.get("state") or "Unknown").strip() or "Unknown"
            sacc = b["states"].setdefault(state, {"gross": 0.0, "shows": 0, "sold": 0, "seats": 0})
            sacc["gross"] += gross
            sacc["shows"] += 1
            sacc["sold"] += sold
            sacc["seats"] += seats

    territories = []
    stats_by_key = {}
    total_gross = 0.0
    total_shows = 0
    total_sold = 0
    total_seats = 0
    for key, b in buckets.items():
        movies_map = b["movies"]
        if not movies_map:
            continue
        t_gross = round(sum(v["gross"] for v in movies_map.values()), 2)
        t_shows = sum(v["shows"] for v in movies_map.values())
        t_sold = sum(v["sold"] for v in movies_map.values())
        t_seats = sum(v["seats"] for v in movies_map.values())
        entry = {
            "key": key,
            "label": b["label"],
            "gross": t_gross,
            "shows": t_shows,
            "sold": t_sold,
            "occupancy": _occ(t_sold, t_seats),
        }
        if b["states"]:
            entry["states"] = sorted(
                (
                    {
                        "state": s, "gross": round(v["gross"], 2), "shows": v["shows"],
                        "sold": v["sold"], "occupancy": _occ(v["sold"], v["seats"]),
                    }
                    for s, v in b["states"].items()
                ),
                key=lambda x: x["gross"], reverse=True,
            )
        territories.append(entry)
        stats_by_key[key] = {"sold": t_sold, "seats": t_seats}
        total_gross += t_gross
        total_shows += t_shows
        total_sold += t_sold
        total_seats += t_seats

    # canonical order: configured territories first (in configured order),
    # then any "other-*" fallback buckets, then Rest of India last.
    order_index = {_slug(name): i for i, name in enumerate(order)}

    def sort_key(t):
        if t["key"] == rest_key:
            return (2, 0)
        if t["key"] in order_index:
            return (0, order_index[t["key"]])
        return (1, t["label"])

    territories.sort(key=sort_key)
    return territories, total_gross, total_shows, total_sold, total_seats, stats_by_key


def _make_report(mode, date_code, territories, total_gross, total_shows, total_sold,
                  total_seats, stats_by_key, rest_key, rest_label, group_defs):
    return {
        "mode": mode,
        "date": date_code,
        "date_iso": f"{date_code[:4]}-{date_code[4:6]}-{date_code[6:8]}",
        "totals": {
            "territories": len(territories),
            "gross": round(total_gross, 2),
            "shows": total_shows,
            "occupancy": _occ(total_sold, total_seats),
        },
        "territories": territories,
        "groups": _compute_groups(territories, stats_by_key, group_defs, rest_key, rest_label),
    }


def build_report(mode, date_code):
    base_root = os.path.join(HERE, mode, "data")
    if not date_code:
        date_code = _latest_date(base_root)
        if not date_code:
            print(f"! no dates found under {base_root}")
            return None

    base_dir = os.path.join(base_root, date_code)
    if not os.path.isdir(base_dir):
        print(f"! {base_dir} does not exist")
        return None

    cfg = load_config()
    groups = build_groups(cfg)
    rest_key = cfg.get("rest_of_india_key", "rest-of-india")
    rest_label = cfg.get("rest_of_india_label", "Rest of India")
    order = cfg.get("territory_order", [])
    group_defs = cfg.get("groups", DEFAULT_GROUPS)

    rows, source = _load_rows(base_dir)
    print(f"  loaded {len(rows)} row(s) from {source}")

    # ---- ALL movies (admin / full raw breakdown) ----
    territories, total_gross, total_shows, total_sold, total_seats, stats_by_key = _aggregate(
        rows, groups, rest_key, rest_label, order
    )
    report = _make_report(mode, date_code, territories, total_gross, total_shows, total_sold,
                           total_seats, stats_by_key, rest_key, rest_label, group_defs)

    out_path = os.path.join(base_dir, "territory.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(f"  {len(territories)} territorie(s), "
          f"gross={report['totals']['gross']}, shows={report['totals']['shows']}")
    print(f"  saved -> {out_path}")

    # ---- TRACKED titles only (matches what the dashboard actually shows) ----
    tracked = load_tracked_titles()
    tracked_rows = rows if tracked[0] == "all" else [r for r in rows if _is_tracked(r.get("movie"), tracked)]
    t_territories, t_gross, t_shows, t_sold, t_seats, t_stats_by_key = _aggregate(
        tracked_rows, groups, rest_key, rest_label, order
    )
    tracked_report = _make_report(mode, date_code, t_territories, t_gross, t_shows, t_sold,
                                   t_seats, t_stats_by_key, rest_key, rest_label, group_defs)

    tracked_out_path = os.path.join(base_dir, "territory_tracked.json")
    with open(tracked_out_path, "w", encoding="utf-8") as f:
        json.dump(tracked_report, f, ensure_ascii=False, indent=2)

    print(f"  tracked-only: {len(t_territories)} territorie(s), "
          f"gross={tracked_report['totals']['gross']}, shows={tracked_report['totals']['shows']}")
    print(f"  saved -> {tracked_out_path}")

    return report


def main():
    ap = argparse.ArgumentParser(description="Build the all-India territory report JSON.")
    ap.add_argument("--mode", choices=["daily", "advance"], default="daily")
    ap.add_argument("--date", default=None, help="YYYYMMDD (defaults to latest available)")
    args = ap.parse_args()

    report = build_report(args.mode, args.date)
    if report is None:
        sys.exit(1)


if __name__ == "__main__":
    main()