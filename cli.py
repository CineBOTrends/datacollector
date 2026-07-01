#!/usr/bin/env python3
"""
CLI entry point for tracktollywood-moviemint-python.

Usage:
    python cli.py run --mode advance --date 20260319
    python cli.py run --mode daily
    python cli.py run --mode both --date 20260320

    python cli.py scrape --mode advance --shard 1 --date 20260319
    python cli.py scrape --mode advance --shard all

    python cli.py combine --mode advance --date 20260319

    python cli.py serve --port 8080

    python cli.py cleanup
"""
import argparse
import sys
import os

# Ensure project root is on path
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


def cmd_scrape(args):
    """Run scraper for specified shard(s)."""
    from scraper.scrape import run_shard

    if args.shard == "all":
        shards = list(range(1, 10))
    else:
        shards = [int(args.shard)]

    for shard_id in shards:
        print(f"\n{'='*60}")
        print(f"Running shard {shard_id} in {args.mode} mode")
        print(f"{'='*60}")
        run_shard(mode=args.mode, shard_id=shard_id, date_code=args.date)


def cmd_combine(args):
    """Combine shard outputs."""
    from combiner.combine import combine_shards

    combine_shards(
        mode=args.mode,
        date_code=args.date,
        upload_r2=not args.no_r2,
    )


def cmd_run(args):
    """Full pipeline: scrape all shards in parallel + combine + cleanup."""
    from runner import run_pipeline

    run_pipeline(mode=args.mode, date_code=args.date)


def cmd_serve(args):
    """Start the API server."""
    from services.api_server import app

    port = args.port
    print(f"Starting API server on port {port}...")
    app.run(host="0.0.0.0", port=port)


def cmd_upload(args):
    """Upload finalsummary.json to R2."""
    import os
    from services.r2_storage import upload_file, is_r2_configured

    if not is_r2_configured():
        print("R2 not configured. Set R2_ENDPOINT, R2_ACCESS_KEY, R2_SECRET_KEY.")
        return

    dc = args.date
    mode = args.mode
    base = "daily" if mode == "daily" else "advance"
    local_path = os.path.join(base, "data", dc, "finalsummary.json")

    if not os.path.exists(local_path):
        print(f"File not found: {local_path}")
        return

    year, month, day = dc[:4], dc[4:6], dc[6:8]
    r2_key = f"v1/{mode}/{year}/{month}/{day}.json"
    upload_file(local_path, r2_key)


def cmd_purge(args):
    """Purge Cloudflare cache for a specific date."""
    from services.hooks import purge_after_r2_upload, is_cf_configured

    if not is_cf_configured():
        print("Cloudflare not configured. Set CF_ZONE_ID, CF_API_TOKEN, CF_PURGE_BASE_URL.")
        return

    dc = args.date
    mode = args.mode
    year, month, day = dc[:4], dc[4:6], dc[6:8]
    r2_key = f"v1/{mode}/{year}/{month}/{day}.json"
    purge_after_r2_upload(r2_key)


def cmd_scheduler(args):
    """Start the cron scheduler."""
    from scheduler import start_scheduler
    start_scheduler(config_path=args.config)


def cmd_cleanup(args):
    """Run shard file cleanup."""
    # Import and run cleanup inline
    from datetime import datetime, timedelta
    import pytz

    IST = pytz.timezone("Asia/Kolkata")
    BASE_PATHS = ["advance/data", "daily/data"]
    START_DATE = (datetime.now(IST) - timedelta(days=5)).strftime("%Y%m%d")
    END_DATE = (datetime.now(IST) - timedelta(days=1)).strftime("%Y%m%d")

    FILES_TO_DELETE = [
        *(f"detailed{i}.json" for i in range(1, 10)),
        *(f"movie_summary{i}.json" for i in range(1, 10)),
    ]

    def daterange(start, end):
        cur = datetime.strptime(start, "%Y%m%d")
        end_dt = datetime.strptime(end, "%Y%m%d")
        while cur <= end_dt:
            yield cur.strftime("%Y%m%d")
            cur += timedelta(days=1)

    deleted = 0
    print(f"Cleaning shard files from {START_DATE} -> {END_DATE} (IST)\n")

    for date in daterange(START_DATE, END_DATE):
        for base in BASE_PATHS:
            folder = os.path.join(base, date)
            if not os.path.isdir(folder):
                continue
            for fname in FILES_TO_DELETE:
                path = os.path.join(folder, fname)
                if os.path.exists(path):
                    os.remove(path)
                    deleted += 1
                    print(f"Deleted: {path}")

    print(f"\nCleanup complete. Files removed: {deleted}")


def main():
    parser = argparse.ArgumentParser(
        description="BookMyShow Advance Tracker - Streamlined Scraper",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # scrape
    p_scrape = subparsers.add_parser("scrape", help="Run scraper for specific shard(s)")
    p_scrape.add_argument("--mode", required=True, choices=["advance", "daily", "rotate"],
                          help="Scrape mode")
    p_scrape.add_argument("--shard", required=True,
                          help="Shard ID (1-9) or 'all'")
    p_scrape.add_argument("--date", default=None,
                          help="Date override (YYYYMMDD)")
    p_scrape.set_defaults(func=cmd_scrape)

    # combine
    p_combine = subparsers.add_parser("combine", help="Combine shard outputs")
    p_combine.add_argument("--mode", required=True, choices=["advance", "daily", "rotate"],
                           help="Combine mode")
    p_combine.add_argument("--date", default=None,
                           help="Date override (YYYYMMDD)")
    p_combine.add_argument("--no-r2", action="store_true",
                           help="Skip R2 upload")
    p_combine.set_defaults(func=cmd_combine)

    # run (full pipeline)
    p_run = subparsers.add_parser("run", help="Full pipeline: scrape all + combine + cleanup")
    p_run.add_argument("--mode", required=True, choices=["advance", "daily", "both"],
                       help="Pipeline mode")
    p_run.add_argument("--date", default=None,
                       help="Date override (YYYYMMDD)")
    p_run.set_defaults(func=cmd_run)

    # serve
    p_serve = subparsers.add_parser("serve", help="Start API server")
    p_serve.add_argument("--port", type=int, default=8080,
                         help="Port number (default: 8080)")
    p_serve.set_defaults(func=cmd_serve)

    # scheduler
    p_sched = subparsers.add_parser("scheduler", help="Start cron scheduler (reads schedule_config.yaml)")
    p_sched.add_argument("--config", default=None,
                         help="Path to schedule config YAML (default: schedule_config.yaml)")
    p_sched.set_defaults(func=cmd_scheduler)

    # upload
    p_upload = subparsers.add_parser("upload", help="Upload finalsummary.json to R2")
    p_upload.add_argument("--mode", required=True, choices=["advance", "daily"],
                          help="Data mode")
    p_upload.add_argument("--date", required=True,
                          help="Date (YYYYMMDD)")
    p_upload.set_defaults(func=cmd_upload)

    # purge
    p_purge = subparsers.add_parser("purge", help="Purge Cloudflare cache for a date")
    p_purge.add_argument("--mode", required=True, choices=["advance", "daily"],
                         help="Data mode")
    p_purge.add_argument("--date", required=True,
                         help="Date (YYYYMMDD)")
    p_purge.set_defaults(func=cmd_purge)

    # cleanup
    p_cleanup = subparsers.add_parser("cleanup", help="Remove old shard files")
    p_cleanup.set_defaults(func=cmd_cleanup)

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    args.func(args)


if __name__ == "__main__":
    main()
