#!/usr/bin/env python3
"""
Multiplex Report builder.
=========================
Builds a compact "Daily Multiplex Report"-style JSON (movies playing, shows,
gross collected) for a hand-picked list of multiplex chains, straight from the
collector's raw show-level rows.

Reads (in order of preference, first one found wins):
    <mode>/data/<date>/finaldetailed.json   {"last_updated":..., "data":[rows]}
    <mode>/data/<date>/detailed*.json       raw per-shard rows (all 9 shards,
                                             concatenated in-memory; used when
                                             the combine step hasn't run yet)

Each row is expected to look like (see scraper/parser.py):
    {"movie":..., "venue":..., "chain":..., "city":..., "state":...,
     "grossRevenue":..., "ticketsSold":..., "time":..., "date":...}

Selected chains + their matching rules live in multiplex_chains.json (root of
the collector) so they can be edited without touching this script.

Writes:
    <mode>/data/<date>/multiplex.json

Usage:
    python multiplex_report.py --mode daily --date 20260920
    python multiplex_report.py --mode advance                 # latest date
    python multiplex_report.py                                 # daily, latest
"""
import argparse
import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CHAINS_FILE = os.path.join(HERE, "multiplex_chains.json")


def load_chain_config():
    with open(CHAINS_FILE, encoding="utf-8") as f:
        cfg = json.load(f)
    chains = []
    for entry in cfg.get("chains", []):
        key = entry.get("key")
        label = entry.get("label", key)
        match = [m.strip().lower() for m in entry.get("match", []) if m and m.strip()]
        # "match_field": "chain" (default, matches the theatre chain/company name)
        # or "location" (matches venue name + address instead — for grouping
        # standalone single-screen theatres by area, e.g. all cinemas at one
        # crossroads/complex that aren't part of the same chain).
        match_field = str(entry.get("match_field", "chain")).strip().lower()
        if match_field not in ("chain", "location", "venue"):
            match_field = "chain"
        # "match_type": "contains" (default) or "exact" (whole-string match,
        # useful for venues so "AMB Cinemas: Gachibowli" never catches
        # "AMB Cinemas: Secunderabad").
        match_type = str(entry.get("match_type", "contains")).strip().lower()
        if match_type not in ("contains", "exact"):
            match_type = "contains"
        # "split_by_venue": true -> one report entry per distinct venue found
        # for this chain (no need to list every venue by hand).
        split = bool(entry.get("split_by_venue", False))
        if key and (match or split):
            chains.append({"key": key, "label": label, "match": match,
                           "match_field": match_field, "match_type": match_type,
                           "split_by_venue": split})
    return chains


def _norm(text):
    """Lowercase + collapse whitespace so spacing differences don't break matches."""
    return " ".join(str(text or "").lower().split())


def _match_chain(row, chains):
    """Return the matching chain config for a row, or None."""
    chain_field = (row.get("chain") or "").strip()
    if not chain_field or chain_field.lower() == "unknown":
        chain_field = (row.get("venue") or "").split(":")[0].strip()
    chain_hay = chain_field.lower()

    venue_field = (row.get("venue") or "").strip()
    address_field = (row.get("address") or "").strip()
    location_hay = f"{venue_field} {address_field}".lower()

    venue_hay = _norm(venue_field)

    for c in chains:
        if c["match_field"] == "venue":
            hay = venue_hay
        elif c["match_field"] == "location":
            hay = location_hay
        else:
            hay = chain_hay
        if c["match_type"] == "exact":
            ok = any(_norm(m) == hay for m in c["match"])
        else:
            ok = any(_norm(m) in hay for m in c["match"])
        if ok:
            return c
    return None


def _load_rows(base_dir):
    """Prefer the deduped finaldetailed.json; fall back to raw shard files."""
    final_path = os.path.join(base_dir, "finaldetailed.json")
    if os.path.exists(final_path):
        with open(final_path, encoding="utf-8") as f:
            payload = json.load(f)
        rows = payload.get("data", []) if isinstance(payload, dict) else payload
        return rows, final_path

    rows = []
    shard_files = sorted(glob.glob(os.path.join(base_dir, "detailed*.json")))
    for fp in shard_files:
        try:
            with open(fp, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                rows.extend(data)
        except Exception as e:
            print(f"  ! could not read {fp}: {e}")
    return rows, f"{len(shard_files)} shard file(s)"


def _latest_date(base_root):
    if not os.path.isdir(base_root):
        return None
    dates = [d for d in os.listdir(base_root)
              if os.path.isdir(os.path.join(base_root, d)) and d.isdigit() and len(d) == 8]
    return max(dates) if dates else None


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

    chains = load_chain_config()
    if not chains:
        print("! multiplex_chains.json has no valid chains configured")
        return None

    rows, source = _load_rows(base_dir)
    print(f"  loaded {len(rows)} row(s) from {source}")

    # bucket_id -> {"cfg", "label", "movies": {movie: {"gross", "shows"}}}
    # bucket_id is the config key, or "<key>::<venue>" for split_by_venue entries.
    buckets = {}

    for r in rows:
        c = _match_chain(r, chains)
        if not c:
            continue
        venue = " ".join((r.get("venue") or "Unknown").split())
        if c["split_by_venue"]:
            bid, label = f"{c['key']}::{venue.lower()}", venue
        else:
            bid, label = c["key"], c["label"]
        b = buckets.setdefault(bid, {"cfg": c, "key": bid, "label": label, "movies": {}})
        movie = r.get("movie", "Unknown")
        gross = float(r.get("grossRevenue", 0) or 0)
        acc = b["movies"].setdefault(movie, {"gross": 0.0, "shows": 0})
        acc["gross"] += gross
        acc["shows"] += 1

    theatres = []
    total_gross = 0.0
    total_shows = 0
    for b in buckets.values():
        movies_map = b["movies"]
        movie_list = [
            {"rank": i + 1, "movie": m, "gross": round(v["gross"], 2), "shows": v["shows"]}
            for i, (m, v) in enumerate(
                sorted(movies_map.items(), key=lambda kv: kv[1]["gross"], reverse=True)
            )
        ]
        gross = round(sum(v["gross"] for v in movies_map.values()), 2)
        shows = sum(v["shows"] for v in movies_map.values())
        theatres.append({
            "key": b["key"],
            "label": b["label"],
            "gross": gross,
            "shows": shows,
            "movies": movie_list,
        })
        total_gross += gross
        total_shows += shows

    # keep configured order; venues inside a split_by_venue entry go alphabetically
    order = {c["key"]: i for i, c in enumerate(chains)}
    theatres.sort(key=lambda t: (order[t["key"].split("::")[0]], t["label"].lower()))

    report = {
        "mode": mode,
        "date": date_code,
        "date_iso": f"{date_code[:4]}-{date_code[4:6]}-{date_code[6:8]}",
        "totals": {
            "theatres": len(theatres),
            "gross": round(total_gross, 2),
            "shows": total_shows,
        },
        "theatres": theatres,
    }

    out_path = os.path.join(base_dir, "multiplex.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(f"  {len(theatres)} matched chain(s), "
          f"gross={report['totals']['gross']}, shows={report['totals']['shows']}")
    print(f"  saved -> {out_path}")
    return report


def main():
    ap = argparse.ArgumentParser(description="Build the multiplex report JSON.")
    ap.add_argument("--mode", choices=["daily", "advance"], default="daily")
    ap.add_argument("--date", default=None, help="YYYYMMDD (defaults to latest available)")
    args = ap.parse_args()

    report = build_report(args.mode, args.date)
    if report is None:
        sys.exit(1)


if __name__ == "__main__":
    main()