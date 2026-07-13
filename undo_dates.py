#!/usr/bin/env python3
"""
Undo a scrape: remove one or more advance/daily dates everywhere.

    python undo_dates.py advance 20260717 20260719 20260721
    python undo_dates.py advance 20260717 --dry-run

Why this exists: deleting the folders is NOT enough. ci_persist.save() pushed
those dates to R2 (raw/<mode>/<date>/...), and ci_persist.restore() pulls back
EVERY key under raw/<mode>/ at the start of the next combine job. Miss the R2
copy and the dates resurrect themselves on the next run and get republished.

Order of operations:
  1. delete the R2 keys        <- the authoritative copy
  2. delete the local folders  <- what build_data.py reads
  3. you commit + push, then run any normal collect so data/ is rebuilt
     WITHOUT those dates and republished to the dashboard.
"""
import os
import shutil
import sys

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


def purge_r2(mode, dates, dry_run=False):
    from services.r2_storage import get_r2_client, is_r2_configured
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
                print(f"  {'[dry] ' if dry_run else ''}R2 delete {key}")
                if not dry_run:
                    client.delete_object(Bucket=bucket, Key=key)
                removed += 1
            if not resp.get("IsTruncated"):
                break
            token = resp.get("NextContinuationToken")
    print(f"R2: {removed} key(s) {'would be ' if dry_run else ''}deleted")
    return removed


def purge_local(mode, dates, dry_run=False):
    removed = 0
    for date in dates:
        d = os.path.join(mode, "data", date)
        if os.path.isdir(d):
            print(f"  {'[dry] ' if dry_run else ''}rm -rf {d}")
            if not dry_run:
                shutil.rmtree(d)
            removed += 1
        else:
            print(f"  (absent) {d}")
    return removed


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    dry = "--dry-run" in sys.argv
    if len(args) < 2:
        print(__doc__)
        sys.exit(1)

    mode, dates = args[0], args[1:]
    if mode not in ("advance", "daily"):
        print("mode must be 'advance' or 'daily'")
        sys.exit(1)

    print(f"Purging {mode} dates: {', '.join(dates)}"
          + ("  (DRY RUN)" if dry else ""))
    print("\n1. R2 (authoritative — restore() would otherwise bring these back)")
    purge_r2(mode, dates, dry)
    print("\n2. local working tree")
    purge_local(mode, dates, dry)

    print("\n3. now commit, push, and re-publish:")
    print("   git add -A && git commit -m 'undo: drop "
          + " ".join(dates) + f" ({mode})' && git push")
    print("   python build_data.py .        # rebuilds data/ without those dates")
    print("   ...then run any normal collect so data/ is published to the dashboard")


if __name__ == "__main__":
    main()
