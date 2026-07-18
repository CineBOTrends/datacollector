#!/usr/bin/env python3
"""
Manage which movies get published to the dashboard.

    python tracked.py list                 # every movie in the collected data
    python tracked.py list --mode daily    # only what's in daily/
    python tracked.py show                 # the current tracked list + status
    python tracked.py add "The Odyssey" Lenin
    python tracked.py remove Lenin
    python tracked.py all                  # switch back to tracking everything
    python tracked.py selected             # switch to list-only mode

Filtering happens DURING THE SCRAPE (scraper/tracked_filter.py). Untracked
movies are dropped as each venue is parsed, so they never reach the shard files,
the combined files, R2 or the dashboard. build_data.py applies the same list
again as a backstop, so switching to a shorter list also hides movies that were
collected earlier.

IMPORTANT TRADE-OFF: this is destructive. A movie that isn't on the list is
never collected, so adding it later gives you NO back-history — tracking starts
from that moment. Add a title BEFORE its bookings open, not after.
"""
import json
import os
import re
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
TRACK_FILE = os.path.join(HERE, "tracked_movies.json")

_TAG_RE = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]\s*$")


def canonical(t):
    """'Lenin (2D - Telugu)' -> 'Lenin'"""
    t = (t or "").strip()
    prev = None
    while prev != t and t:
        prev = t
        t = _TAG_RE.sub("", t).strip()
    return t


def slugify(t):
    return re.sub(r"[^a-zA-Z0-9]+", "-", (t or "").lower()).strip("-") or "movie"


def load():
    if not os.path.exists(TRACK_FILE):
        return {"mode": "all", "movies": []}
    try:
        with open(TRACK_FILE, encoding="utf-8") as f:
            cfg = json.load(f)
        cfg.setdefault("mode", "selected")
        cfg.setdefault("movies", [])
        return cfg
    except Exception as e:
        print(f"! {TRACK_FILE} unreadable ({e}) — treating as 'all'")
        return {"mode": "all", "movies": []}


def save(cfg):
    cfg["_comment"] = (
        "mode 'all' publishes every movie; 'selected' publishes only the list "
        "below. Titles OR slugs, format suffixes not needed: 'Lenin' matches "
        "'Lenin (2D - Telugu)'. Raw data always keeps every movie, so a title "
        "added later still has its full history."
    )
    ordered = {"_comment": cfg["_comment"], "mode": cfg["mode"],
               "movies": sorted(set(cfg["movies"]))}
    with open(TRACK_FILE, "w", encoding="utf-8") as f:
        json.dump(ordered, f, ensure_ascii=False, indent=2)
    print(f"saved {TRACK_FILE}  (mode={ordered['mode']}, "
          f"{len(ordered['movies'])} movie(s))")


def collected_titles(mode_filter=None):
    """Every distinct movie across the collected data, newest date first."""
    seen = defaultdict(set)          # canonical title -> {dates}
    for mode in ("daily", "advance"):
        if mode_filter and mode != mode_filter:
            continue
        root = os.path.join(HERE, mode, "data")
        if not os.path.isdir(root):
            continue
        for date in sorted(os.listdir(root), reverse=True):
            fp = os.path.join(root, date, "finalsummary.json")
            if not os.path.exists(fp):
                fp = os.path.join(root, date, "finaldetailed.json")
            if not os.path.exists(fp):
                continue
            try:
                with open(fp, encoding="utf-8") as f:
                    payload = json.load(f)
                rows = payload.get("data", payload) if isinstance(payload, dict) else payload
                for r in rows or []:
                    t = canonical(r.get("movie", ""))
                    if t:
                        seen[t].add(f"{mode[:3]}:{date}")
            except Exception:
                continue
    return seen


def cmd_list(args):
    mode_filter = None
    if "--mode" in args:
        i = args.index("--mode")
        if i + 1 < len(args):
            mode_filter = args[i + 1]

    seen = collected_titles(mode_filter)
    if not seen:
        print("No collected data found. Run a scrape first, or run this in the "
              "collector directory.")
        return

    cfg = load()
    keys = {canonical(m).casefold() for m in cfg["movies"]} | \
           {slugify(m) for m in cfg["movies"]}
    tracking_all = cfg["mode"] == "all"

    print(f"{len(seen)} movie(s) in the collected data"
          + (f"  [{mode_filter}]" if mode_filter else ""))
    print(f"mode: {cfg['mode']}\n")
    for title in sorted(seen, key=lambda t: (-len(seen[t]), t.lower())):
        on = tracking_all or canonical(title).casefold() in keys or slugify(title) in keys
        mark = "[x]" if on else "[ ]"
        print(f"  {mark} {title:<42} {len(seen[title]):>3} date(s)   {slugify(title)}")
    if tracking_all:
        print("\n  (mode=all — everything is published regardless of the list)")


def cmd_show(_args):
    cfg = load()
    print(f"mode : {cfg['mode']}")
    if cfg["mode"] == "all":
        print("       every movie is published")
    if cfg["movies"]:
        print(f"list : {len(cfg['movies'])} movie(s)")
        for m in sorted(cfg["movies"]):
            print(f"   - {m}")
    else:
        print("list : (empty)")
        if cfg["mode"] == "selected":
            print("\n! mode=selected with an empty list would publish an EMPTY "
                  "dashboard.\n  build_data.py detects this and falls back to "
                  "tracking ALL movies.")


def cmd_add(args):
    if not args:
        print("usage: python tracked.py add \"The Odyssey\" Lenin")
        return
    cfg = load()
    cfg["movies"] = list(cfg["movies"]) + [a.strip() for a in args if a.strip()]
    if cfg["mode"] == "all":
        cfg["mode"] = "selected"
        print("mode switched to 'selected'")
        print("  NOTE: from the next scrape, ONLY these movies are collected.")
        print("  Anything not listed is not stored at all, so it will have no")
        print("  history if you add it later.")
    save(cfg)


def cmd_remove(args):
    if not args:
        print("usage: python tracked.py remove Lenin")
        return
    cfg = load()
    drop = {canonical(a).casefold() for a in args} | {slugify(a) for a in args}
    before = len(cfg["movies"])
    cfg["movies"] = [
        m for m in cfg["movies"]
        if canonical(m).casefold() not in drop and slugify(m) not in drop
    ]
    print(f"removed {before - len(cfg['movies'])} entry(ies)")
    save(cfg)


def cmd_all(_args):
    cfg = load()
    cfg["mode"] = "all"
    save(cfg)
    print("now publishing EVERY movie (the list is kept for later)")


def cmd_selected(_args):
    cfg = load()
    if not cfg["movies"]:
        print("! the list is empty — add movies first, or the build would fall "
              "back to tracking all")
    cfg["mode"] = "selected"
    save(cfg)


CMDS = {"list": cmd_list, "show": cmd_show, "add": cmd_add,
        "remove": cmd_remove, "all": cmd_all, "selected": cmd_selected}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in CMDS:
        print(__doc__)
        sys.exit(1)
    CMDS[sys.argv[1]](sys.argv[2:])


if __name__ == "__main__":
    main()
