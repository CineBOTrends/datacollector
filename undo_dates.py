#!/usr/bin/env python3
"""
Undo a scrape: remove one or more advance/daily dates everywhere — either
entirely, or (with --movie) just one movie's data for those dates, leaving
every other movie on that date untouched.

    python undo_dates.py advance 20260717 20260719 20260721
    python undo_dates.py advance 20260717 --dry-run

    # surgical: only this movie's rows/entries for this date, e.g. a
    # glitched Day 1 for one title that shouldn't have tracked yet
    python undo_dates.py daily 20260722 --movie "Jana Nayagan"
    python undo_dates.py daily 20260722 --movie "Jana Nayagan" --dry-run

Why this exists: deleting the folders is NOT enough. ci_persist.save() pushed
those dates to R2 (raw/<mode>/<date>/...), and ci_persist.restore() pulls back
EVERY key under raw/<mode>/ at the start of the next combine job. Miss the R2
copy and the dates resurrect themselves on the next run and get republished.

--movie matching: case-insensitive, matches the full scraped title ("Jana
Nayagan (2D - Tamil)") or the title with its trailing "(format - language)"
tag stripped off ("Jana Nayagan"), or a substring of either. Pass the plainest
form of the title you can — "Jana Nayagan", not a slug — since the raw shard
rows store titles, not slugs.

Order of operations:
  whole-date (no --movie):
    1. delete the R2 keys        <- the authoritative copy
    2. delete the local folders  <- what build_data.py reads
  --movie (surgical):
    1. rewrite each shard/combined JSON in R2 with that movie's rows/entries
       removed, re-uploading it (a date is never fully deleted this way,
       even if it now has zero movies left in it — see the warning that
       prints in that case, and re-run without --movie to drop the date)
    2. same rewrite, locally
  either way, then:
    3. you commit + push, then run any normal collect so data/ is rebuilt
       WITHOUT that data and republished to the dashboard.
"""
import argparse
import json
import os
import re
import shutil
import sys

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# Files combine.py/scrape runs leave directly under <mode>/data/<date>/ that
# can contain per-movie rows or entries: the numbered raw shards plus the
# combined output. Every one of these needs the movie stripped out of it,
# both locally and in R2 — missing one is exactly the "it comes back" bug
# this script exists to avoid.
PER_DATE_JSON_GLOB = re.compile(
    r"^(detailed\d+|movie_summary\d+|finaldetailed|finalsummary)\.json$"
)

# Keys, anywhere in the JSON tree, whose value is a list of per-movie
# records worth filtering. Covers plain lists (shards), {"movies": [...]}
# (national/finalsummary-style), and similar wrapper shapes without needing
# to know each file's exact schema up front.
LIST_CONTAINER_KEYS = ("movies", "results", "data", "rows")

# Row/entry key names that hold a movie's display title, checked in order.
TITLE_KEYS = ("movie", "title", "name")


# --------------------------------------------------------------- matching

def _canonical(title):
    """'Jana Nayagan (2D - Tamil)' -> 'jana nayagan' — strip a trailing
    parenthetical format/language tag and lowercase, so --movie doesn't have
    to be typed with the exact format/language suffix scraped rows carry."""
    t = re.sub(r"\s*\([^)]*\)\s*$", "", title or "").strip().lower()
    return t


def _movie_matches(record_title, query):
    if not record_title:
        return False
    full = record_title.strip().lower()
    base = _canonical(record_title)
    q = query.strip().lower()
    return q == full or q == base or q in full


# ------------------------------------------------------------- JSON filtering

def _filter_movie(obj, query, removed):
    """Recursively drop any dict (in a list, or under a
    LIST_CONTAINER_KEYS key) whose title matches `query`. `removed` is a
    single-element list used as an int counter (mutable, so recursive calls
    share it)."""
    if isinstance(obj, list):
        kept = []
        for item in obj:
            if isinstance(item, dict):
                title = next((item[k] for k in TITLE_KEYS if item.get(k)), None)
                if title and _movie_matches(title, query):
                    removed[0] += 1
                    continue
                item = _filter_movie(item, query, removed)
            kept.append(item)
        return kept
    if isinstance(obj, dict):
        return {k: (_filter_movie(v, query, removed) if k in LIST_CONTAINER_KEYS else v)
                for k, v in obj.items()}
    return obj


def _rewrite_json_bytes(raw_bytes, query):
    """Returns (new_bytes_or_None, removed_count). None means "no change,
    don't rewrite this file." Malformed/non-JSON content is left untouched
    (returns None, 0) rather than raising, since a shard directory can
    legitimately hold files this script doesn't need to touch."""
    try:
        obj = json.loads(raw_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, 0
    removed = [0]
    new_obj = _filter_movie(obj, query, removed)
    if removed[0] == 0:
        return None, 0
    return json.dumps(new_obj, ensure_ascii=False).encode("utf-8"), removed[0]


# --------------------------------------------------------------------- R2

def purge_r2(mode, dates, dry_run=False, movie=None):
    try:
        from services.r2_storage import get_r2_client, is_r2_configured
    except ImportError:
        print("! services.r2_storage not importable here (not run from the "
              "project root?) — run this in CI, or the dates will come back "
              "on the next restore()")
        return 0
    if not is_r2_configured():
        print("! R2 not configured here — run this in CI, or the dates will "
              "come back on the next restore()")
        return 0

    bucket = os.environ.get("R2_BUCKET_NAME")
    client = get_r2_client()
    removed = 0
    for date in dates:
        prefix = f"raw/{mode}/{date}/"
        token = None
        while True:
            kw = {"Bucket": bucket, "Prefix": prefix}
            if token:
                kw["ContinuationToken"] = token
            resp = client.list_objects_v2(**kw)
            for obj in resp.get("Contents", []):
                key = obj["Key"]
                if movie is None:
                    print(f"  {'[dry] ' if dry_run else ''}R2 delete {key}")
                    if not dry_run:
                        client.delete_object(Bucket=bucket, Key=key)
                    removed += 1
                    continue

                if not PER_DATE_JSON_GLOB.match(os.path.basename(key)):
                    continue
                body = client.get_object(Bucket=bucket, Key=key)["Body"].read()
                new_bytes, n = _rewrite_json_bytes(body, movie)
                if new_bytes is None:
                    continue
                print(f"  {'[dry] ' if dry_run else ''}R2 rewrite {key} "
                      f"(-{n} '{movie}' row/entry(s))")
                if not dry_run:
                    client.put_object(Bucket=bucket, Key=key, Body=new_bytes)
                removed += n
            if not resp.get("IsTruncated"):
                break
            token = resp.get("NextContinuationToken")
    verb = "deleted" if movie is None else f"'{movie}' row/entry(s) removed"
    print(f"R2: {removed} {verb} {'would be ' if dry_run else ''}")
    return removed


# ------------------------------------------------------------------ local

def purge_local(mode, dates, dry_run=False, movie=None):
    removed = 0
    for date in dates:
        d = os.path.join(mode, "data", date)
        if not os.path.isdir(d):
            print(f"  (absent) {d}")
            continue

        if movie is None:
            print(f"  {'[dry] ' if dry_run else ''}rm -rf {d}")
            if not dry_run:
                shutil.rmtree(d)
            removed += 1
            continue

        touched_any = False
        for fname in sorted(os.listdir(d)):
            if not PER_DATE_JSON_GLOB.match(fname):
                continue
            fp = os.path.join(d, fname)
            with open(fp, "rb") as f:
                new_bytes, n = _rewrite_json_bytes(f.read(), movie)
            if new_bytes is None:
                continue
            touched_any = True
            print(f"  {'[dry] ' if dry_run else ''}{fp} (-{n} '{movie}' row/entry(s))")
            if not dry_run:
                with open(fp, "wb") as f:
                    f.write(new_bytes)
            removed += n
        if not touched_any:
            print(f"  (no '{movie}' rows/entries found under {d})")
    return removed


# ---------------------------------------------------------------------- cli

def main():
    ap = argparse.ArgumentParser(
        add_help=False,  # keep the module docstring as the --help text below
        usage="undo_dates.py <advance|daily> DATE [DATE ...] [--movie TITLE] [--dry-run]",
    )
    ap.add_argument("mode", nargs="?", choices=["advance", "daily"])
    ap.add_argument("dates", nargs="*")
    ap.add_argument("--movie", default=None,
                     help="only remove this movie's data for the given date(s), "
                          "leaving every other movie on those dates untouched")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("-h", "--help", action="store_true")
    a = ap.parse_args()

    if a.help or not a.mode or not a.dates:
        print(__doc__)
        sys.exit(0 if a.help else 1)

    mode, dates, movie, dry = a.mode, a.dates, a.movie, a.dry_run

    scope = f"movie '{movie}'" if movie else "ALL movies"
    print(f"Purging {mode} dates: {', '.join(dates)} — {scope}"
          + ("  (DRY RUN)" if dry else ""))
    print("\n1. R2 (authoritative — restore() would otherwise bring these back)")
    purge_r2(mode, dates, dry, movie)
    print("\n2. local working tree")
    purge_local(mode, dates, dry, movie)

    if movie:
        print(f"\n! this only removed '{movie}' — if that left a date with zero "
              f"movies in it, re-run without --movie for that date to drop it "
              f"entirely instead of publishing an empty day.")

    print("\n3. now commit, push, and re-publish:")
    label = (f"{movie} " if movie else "") + " ".join(dates)
    print("   git add -A && git commit -m 'undo: drop "
          + label + f" ({mode})' && git push")
    print("   python build_data.py .        # rebuilds data/ without those dates")
    print("   ...then run any normal collect so data/ is published to the dashboard")


if __name__ == "__main__":
    main()