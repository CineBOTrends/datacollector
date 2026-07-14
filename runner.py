#!/usr/bin/env python3
"""
Orchestrator - Runs all 9 shards in parallel, then combines.
"""
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

# Ensure project root is on path
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from services.logger import get_logger

IST = timezone(timedelta(hours=5, minutes=30))
logger = get_logger(shard_id=None, log_file=None)


def _run_one_shard(mode, shard_id, date_code):
    """Run a single shard, return (success, shard_id, duration)."""
    start = time.time()
    try:
        from scraper.scrape import run_shard
        run_shard(mode=mode, shard_id=shard_id, date_code=date_code)
        duration = time.time() - start
        return True, shard_id, duration
    except Exception as e:
        duration = time.time() - start
        logger.error(f"Shard {shard_id} failed: {e}")
        import traceback
        traceback.print_exc()
        return False, shard_id, duration


def run_advance(date_code=None):
    """Run advance booking scrapers (all 9 shards parallel + combine)."""
    from scraper.config import get_config

    config = get_config("advance", date_code)
    dc = config["date_code"]

    logger.separator("=")
    logger.info("ADVANCE BOOKING SCRAPE")
    logger.separator("=")
    logger.info(f"Date: {dc}")

    # Run all shards in parallel
    with ThreadPoolExecutor(max_workers=9) as executor:
        futures = {
            executor.submit(_run_one_shard, "advance", i, dc): i
            for i in range(1, 10)
        }
        results = []
        for future in as_completed(futures):
            results.append(future.result())

    # Wait before combining
    logger.wait("Waiting 10s before combining shards...")
    time.sleep(10)

    # Combine
    logger.info("Combining advance shards...")
    from combiner.combine import combine_shards
    combine_shards(mode="advance", date_code=dc)

    # Cleanup
    logger.info("Cleaning up shard files...")
    _cleanup()

    success_count = sum(1 for s, _, _ in results if s)
    logger.success(f"Advance scrape: {success_count}/9 shards successful")

    return results


def run_daily():
    """Run daily scrapers (all 9 shards parallel + combine)."""
    logger.separator("=")
    logger.info("DAILY SCRAPE (TODAY)")
    logger.separator("=")

    with ThreadPoolExecutor(max_workers=9) as executor:
        futures = {
            executor.submit(_run_one_shard, "daily", i, None): i
            for i in range(1, 10)
        }
        results = []
        for future in as_completed(futures):
            results.append(future.result())

    logger.wait("Waiting 10s before combining daily shards...")
    time.sleep(10)

    logger.info("Combining daily shards...")
    from combiner.combine import combine_shards
    combine_shards(mode="daily")

    success_count = sum(1 for s, _, _ in results if s)
    logger.success(f"Daily scrape: {success_count}/9 shards successful")

    return results


def run_upcoming(window_days=None, force_probe=False):
    """Opening-day advance for films that haven't released yet.

    Discovers each upcoming film's RELEASE DATE (from District's movieInfo) and
    scrapes advance bookings for that exact day — the opening-day figure — rather
    than arbitrary D+3/5/7 snapshots.
    """
    from upcoming import discover_opening_days, filter_to_opening

    logger.separator("=")
    logger.info("UPCOMING RELEASES — OPENING DAY ADVANCE")
    logger.separator("=")

    opening = discover_opening_days(window_days=window_days, force=force_probe)
    if not opening:
        logger.info("No upcoming releases with open bookings. Nothing to do.")
        return []

    logger.info(f"{len(opening)} opening day(s) to collect:")
    for dc, titles in sorted(opening.items()):
        logger.info(f"  {dc}: {', '.join(titles)}")

    results = []
    for dc, titles in sorted(opening.items()):
        logger.info(f"--- opening day {dc} ---")
        try:
            results.extend(run_advance(dc))
            filter_to_opening(dc, titles)
        except Exception as e:
            logger.error(f"opening day {dc} failed: {e}")

    return results


def _cleanup():
    """Run shard file cleanup."""
    from datetime import datetime
    import pytz

    IST_TZ = pytz.timezone("Asia/Kolkata")
    BASE_PATHS = ["advance/data", "daily/data"]
    START_DATE = (datetime.now(IST_TZ) - timedelta(days=5)).strftime("%Y%m%d")
    END_DATE = (datetime.now(IST_TZ) - timedelta(days=1)).strftime("%Y%m%d")

    FILES_TO_DELETE = [
        *(f"detailed{i}.json" for i in range(1, 10)),
        *(f"movie_summary{i}.json" for i in range(1, 10)),
    ]

    deleted = 0
    cur = datetime.strptime(START_DATE, "%Y%m%d")
    end = datetime.strptime(END_DATE, "%Y%m%d")
    while cur <= end:
        date = cur.strftime("%Y%m%d")
        for base in BASE_PATHS:
            folder = os.path.join(base, date)
            if os.path.isdir(folder):
                for fname in FILES_TO_DELETE:
                    path = os.path.join(folder, fname)
                    if os.path.exists(path):
                        os.remove(path)
                        deleted += 1
        cur += timedelta(days=1)

    if deleted:
        logger.info(f"Cleaned up {deleted} old shard files")


def run_pipeline(mode: str = "both", date_code: str = None):
    """
    Main pipeline entry point.

    Args:
        mode: "advance", "daily", or "both"
        date_code: Optional YYYYMMDD override
    """
    logger.start("BMS Scraper Runner Started")
    overall_start = time.time()

    try:
        if mode in ["advance", "both"]:
            run_advance(date_code)

        if mode in ["daily", "both"]:
            run_daily()

        if mode in ["upcoming", "all"]:
            run_upcoming()

        if mode == "all":
            run_advance(date_code)
            run_daily()

        duration = time.time() - overall_start
        logger.separator("=")
        logger.done(f"All scraping completed in {duration / 60:.1f} minutes")
        logger.separator("=")

    except Exception as e:
        logger.critical(f"Fatal error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    mode = os.environ.get("SCRAPE_MODE", "both")
    run_pipeline(mode=mode)
