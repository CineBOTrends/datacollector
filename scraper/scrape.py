"""
Unified scraper entry point.
Replaces 27 individual scraper files with one parameterized function.
"""
import json
import os
import sys
import random
import time
import asyncio
from datetime import datetime

from scraper.config import get_config, IST
from scraper.dedupe import dedupe
from scraper.daily_merge import minutes_left_sync, merge_with_old_sync, merge_with_old_async


def _get_logger(shard_id, log_file):
    """Get logger, handling import from services."""
    from services.logger import get_logger
    return get_logger(shard_id=shard_id, log_file=log_file)


def _resolve_venue_path(shard_id, project_root):
    """Resolve path to venue JSON file."""
    if shard_id == 9:
        return os.path.join(project_root, "venues", "districtvenues.json")
    else:
        return os.path.join(project_root, "venues", f"venues{shard_id}.json")


def run_shard(mode: str, shard_id: int, date_code: str = None):
    """
    Run a single scraper shard.

    Args:
        mode: "advance", "daily", or "rotate"
        shard_id: 1-9
        date_code: Optional YYYYMMDD date override
    """
    config = get_config(mode, date_code)
    dc = config["date_code"]
    base_dir = config["base_dir"]
    log_dir = os.path.join(base_dir, "logs")
    os.makedirs(log_dir, exist_ok=True)

    detailed_file = f"{base_dir}/detailed{shard_id}.json"
    summary_file = f"{base_dir}/movie_summary{shard_id}.json"

    # Log file name matches original convention
    if shard_id == 9:
        if config["is_daily"]:
            log_file = f"{log_dir}/districtdaily{shard_id}.log"
        else:
            log_file = f"{log_dir}/district{shard_id}.log"
    else:
        if config["is_daily"]:
            log_file = f"{log_dir}/bmsdaily{shard_id}.log"
        else:
            log_file = f"{log_dir}/bms{shard_id}.log"

    logger = _get_logger(shard_id, log_file)

    # State the tracking scope up front so it's visible in the Actions log —
    # otherwise a mis-typed title looks like "the scrape found nothing".
    from scraper.tracked_filter import load_tracked, describe
    if load_tracked()[0] != "all":
        logger.info(describe())

    if shard_id == 9:
        _run_shard_async(config, shard_id, detailed_file, summary_file, logger)
    else:
        _run_shard_sync(config, shard_id, detailed_file, summary_file, logger)


# =====================================================
# SYNC SHARD (1-8)
# =====================================================
def _run_shard_sync(config, shard_id, detailed_file, summary_file, logger):
    from scraper.fetcher_sync import fetch_venue
    from scraper.parser import parse_bms
    from scraper.stealth import reset_identity

    dc = config["date_code"]
    is_daily = config["is_daily"]
    cutoff_minutes = config["cutoff_minutes"]

    if is_daily:
        logger.start("BMS DAILY TRACKER STARTED")
    else:
        logger.start("SCRIPT STARTED")

    venue_path = _resolve_venue_path(shard_id, config["project_root"])
    with open(venue_path, "r", encoding="utf-8") as f:
        venues = json.load(f)

    # Error tracking
    error_counts = {
        "rate_limit": 0,
        "blocked": 0,
        "timeout": 0,
        "network": 0,
        "parse": 0,
        "other": 0,
        "success": 0,
    }
    failed_venues = []
    all_rows = []

    total_venues = len(venues)
    for i, vcode in enumerate(venues, 1):
        if i == 1 or i == total_venues or i % 50 == 0:
            logger.progress(f"[{i}/{total_venues}] Processing venues...")
        try:
            raw = fetch_venue(vcode, dc, logger)
            rows = parse_bms(raw, dc)

            if is_daily:
                # Apply cutoff and add minsLeft
                for r in rows:
                    mins = minutes_left_sync(r["time"])
                    if mins <= cutoff_minutes:
                        r["minsLeft"] = round(mins, 1)
                        r["city"] = venues[vcode].get("City", "Unknown")
                        r["state"] = venues[vcode].get("State", "Unknown")
                        r["source"] = "BMS"
                        r["date"] = dc
                        all_rows.append(r)
            else:
                for r in rows:
                    r["city"] = venues[vcode].get("City", "Unknown")
                    r["state"] = venues[vcode].get("State", "Unknown")
                    r["source"] = "BMS"
                    r["date"] = dc
                all_rows.extend(rows)

            error_counts["success"] += 1

        except Exception as e:
            reset_identity(logger)
            error_msg = str(e)
            error_type = type(e).__name__

            if "RateLimit" in error_msg or "429" in error_msg:
                error_counts["rate_limit"] += 1
                logger.rate_limit(f"{vcode} | Rate Limited (429)")
                failed_venues.append({"venue": vcode, "error": "Rate Limit (429)"})
            elif "Blocked" in error_msg or "403" in error_msg or "HTML" in error_msg:
                error_counts["blocked"] += 1
                logger.error(f"{vcode} | Blocked/Forbidden: {error_msg}")
                failed_venues.append({"venue": vcode, "error": f"Blocked ({error_msg})"})
            elif "TimeoutError" in error_type or "timeout" in error_msg.lower():
                error_counts["timeout"] += 1
                logger.error(f"{vcode} | Timeout: {error_type}")
                failed_venues.append({"venue": vcode, "error": f"Timeout ({error_type})"})
            elif "ConnectionError" in error_type or "HTTPError" in error_msg:
                error_counts["network"] += 1
                logger.error(f"{vcode} | Network: {error_type} - {error_msg}")
                failed_venues.append({"venue": vcode, "error": f"Network ({error_type})"})
            elif "JSONDecodeError" in error_type:
                error_counts["parse"] += 1
                logger.error(f"{vcode} | JSON Parse Error")
                failed_venues.append({"venue": vcode, "error": "JSON Parse Error"})
            else:
                error_counts["other"] += 1
                logger.error(f"{vcode} | {error_type}: {error_msg}")
                failed_venues.append({"venue": vcode, "error": f"{error_type}: {error_msg}"})

        time.sleep(random.uniform(0.35, 0.7))

    # Keep only tracked movies. Done BEFORE dedupe/merge/summary so untracked
    # titles never reach the shard files, the combined files, R2 or the site.
    from scraper.tracked_filter import filter_rows, load_tracked
    if load_tracked()[0] != "all":
        all_rows = filter_rows(all_rows, logger, f" (shard {shard_id})")

    # Daily: merge with old data
    if is_daily:
        detailed = merge_with_old_sync(all_rows, detailed_file)
    else:
        logger.info("Deduping shows")
        detailed = dedupe(all_rows)

    # merge_with_old_sync re-introduces yesterday's rows from the existing file,
    # which may predate the tracked list — filter again after the merge.
    if is_daily and load_tracked()[0] != "all":
        detailed = filter_rows(detailed, logger, " (post-merge)")

    # Build summary
    if is_daily:
        from scraper.summary import build_summary_daily_sync
        final_summary = build_summary_daily_sync(detailed)
    else:
        from scraper.summary import build_summary_with_city_details
        final_summary = build_summary_with_city_details(detailed)

    # Save files
    _save_files(detailed, final_summary, detailed_file, summary_file, logger)

    # Print stats
    _print_stats_sync(
        logger, shard_id, len(venues), error_counts, failed_venues,
        detailed, final_summary, all_rows, is_daily
    )


# =====================================================
# ASYNC SHARD (9)
# =====================================================
def _run_shard_async(config, shard_id, detailed_file, summary_file, logger):
    asyncio.run(_run_shard_async_impl(config, shard_id, detailed_file, summary_file, logger))


async def _run_shard_async_impl(config, shard_id, detailed_file, summary_file, logger):
    from scraper.fetcher_async import fetch_all_async

    mode = config["mode"]
    dc = config["date_code"]
    is_daily = config["is_daily"]

    if is_daily:
        logger.start("DISTRICT DAILY SCRAPER STARTED")
    else:
        logger.start("DISTRICT SCRAPER STARTED")

    logger.info(f"Mode={mode}")

    venue_path = _resolve_venue_path(shard_id, config["project_root"])
    with open(venue_path, "r", encoding="utf-8") as f:
        dist_venues = json.load(f)

    logger.info(f"Loaded {len(dist_venues)} district venues")

    # Fetch all venues async
    results, error_counts, failed_venues = await fetch_all_async(
        dist_venues, config["date_district"], mode, logger
    )

    # Parse results (mode-aware)
    if is_daily:
        import pytz
        now_ist = datetime.now(pytz.timezone("Asia/Kolkata"))
        from scraper.parser import parse_district_daily
        fresh = parse_district_daily(
            results, dc, config["cutoff_minutes"], now_ist
        )
        from scraper.tracked_filter import filter_rows, load_tracked
        if load_tracked()[0] != "all":
            fresh = filter_rows(fresh, logger, " (district)")
        # Merge with old data
        detailed = merge_with_old_async(fresh, detailed_file)
        # the merge pulls back earlier rows that may predate the tracked list
        if load_tracked()[0] != "all":
            detailed = filter_rows(detailed, logger, " (post-merge)")
        # Build summary
        from scraper.summary import build_summary_daily_async
        final_summary = build_summary_daily_async(detailed)
    else:
        from scraper.parser import parse_district_advance
        detailed = parse_district_advance(results, dc)
        from scraper.tracked_filter import filter_rows, load_tracked
        if load_tracked()[0] != "all":
            detailed = filter_rows(detailed, logger, " (district)")
        # Build summary
        from scraper.summary import build_summary_with_city_details
        final_summary = build_summary_with_city_details(detailed)

    # Save files
    _save_files(detailed, final_summary, detailed_file, summary_file, logger)

    # Print stats
    _print_stats_async(
        logger, shard_id, len(dist_venues), error_counts, failed_venues,
        detailed, final_summary, is_daily
    )


# =====================================================
# FILE SAVE
# =====================================================
def _save_files(detailed, summary, detailed_file, summary_file, logger):
    try:
        os.makedirs(os.path.dirname(detailed_file), exist_ok=True)

        with open(detailed_file, "w", encoding="utf-8") as f:
            json.dump(detailed, f, indent=2, ensure_ascii=False)
        if os.path.exists(detailed_file):
            size = os.path.getsize(detailed_file)
            logger.success(f"Saved {detailed_file} ({size} bytes)")
        else:
            logger.error(f"FAILED to save {detailed_file}")

        with open(summary_file, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2, ensure_ascii=False)
        if os.path.exists(summary_file):
            size = os.path.getsize(summary_file)
            logger.success(f"Saved {summary_file} ({size} bytes)")
        else:
            logger.error(f"FAILED to save {summary_file}")

    except Exception as e:
        logger.critical(f"ERROR saving files: {e}")
        import traceback
        traceback.print_exc()


# =====================================================
# STATS PRINTING
# =====================================================
def _print_stats_sync(logger, shard_id, total_venues, error_counts, failed_venues,
                      detailed, final_summary, all_rows, is_daily):
    logger.separator("=", 70)
    logger.stats("EXECUTION SUMMARY")
    logger.separator("-", 70)

    total_errors = sum(error_counts[k] for k in error_counts if k != "success")
    success_rate = (error_counts["success"] / total_venues * 100) if total_venues > 0 else 0

    logger.stats(f"Total Venues: {total_venues}")
    logger.stats(f"Successful: {error_counts['success']} ({success_rate:.1f}%)")
    logger.stats(f"Failed: {total_errors} ({100 - success_rate:.1f}%)")
    logger.separator("-", 70)

    logger.stats("ERROR BREAKDOWN:")
    logger.stats(f"  Rate Limit (429):     {error_counts['rate_limit']}")
    logger.stats(f"  Blocked/Forbidden:    {error_counts['blocked']}")
    logger.stats(f"  Timeout Errors:       {error_counts['timeout']}")
    logger.stats(f"  Network Errors:       {error_counts['network']}")
    logger.stats(f"  Parse Errors:         {error_counts['parse']}")
    logger.stats(f"  Other Errors:         {error_counts['other']}")
    logger.separator("-", 70)

    logger.stats("DATA COLLECTED:")
    logger.stats(f"  Total Shows:          {len(detailed)}")
    logger.stats(f"  Unique Movies:        {len(final_summary)}")
    if not is_daily:
        logger.stats(f"  Shows Before Dedupe:  {len(all_rows)}")
        logger.stats(f"  Duplicates Removed:   {len(all_rows) - len(detailed)}")

    if failed_venues:
        logger.separator("-", 70)
        logger.warn(f"FAILED VENUES ({len(failed_venues)}):")
        for fv in failed_venues[:10]:
            logger.warn(f"  {fv['venue']}: {fv['error']}")
        if len(failed_venues) > 10:
            logger.warn(f"  ... and {len(failed_venues) - 10} more")

    logger.separator("=", 70)
    if is_daily:
        logger.done(f"DONE | Shows={len(detailed)} | Movies={len(final_summary)}")
    else:
        logger.done(f"SHARD {shard_id} COMPLETE | Shows={len(detailed)} | Movies={len(final_summary)} | Success Rate={success_rate:.1f}%")


def _print_stats_async(logger, shard_id, total_venues, error_counts, failed_venues,
                       detailed, final_summary, is_daily):
    logger.separator("=", 70)
    logger.stats("EXECUTION SUMMARY (DISTRICT API)")
    logger.separator("-", 70)

    total_errors = sum(error_counts[k] for k in error_counts if k != "success")
    success_rate = (error_counts["success"] / total_venues * 100) if total_venues > 0 else 0

    logger.stats(f"Total Venues: {total_venues}")
    logger.stats(f"Successful: {error_counts['success']} ({success_rate:.1f}%)")
    logger.stats(f"Failed: {total_errors} ({100 - success_rate:.1f}%)")
    logger.separator("-", 70)

    logger.stats("ERROR BREAKDOWN (DISTRICT):")
    logger.stats(f"  Rate Limit (429):     {error_counts['rate_limit']}")
    logger.stats(f"  Blocked/Forbidden:    {error_counts['blocked']}")
    logger.stats(f"  Server Errors (5xx):  {error_counts['server_error']}")
    logger.stats(f"  HTTP Errors (Other):  {error_counts['http_error']}")
    logger.stats(f"  Timeout Errors:       {error_counts['timeout']}")
    logger.stats(f"  Network Errors:       {error_counts['network']}")
    logger.stats(f"  No Shows (Date N/A):  {error_counts['no_shows']}")
    logger.stats(f"  Other Errors:         {error_counts['other']}")
    logger.separator("-", 70)

    logger.stats("DATA COLLECTED:")
    logger.stats(f"  Total Shows:          {len(detailed)}")
    logger.stats(f"  Unique Movies:        {len(final_summary)}")

    if failed_venues:
        logger.separator("-", 70)
        logger.warn(f"FAILED VENUES ({len(failed_venues)}):")
        for fv in failed_venues[:10]:
            logger.warn(f"  {fv['venue']}: {fv['error']}")
        if len(failed_venues) > 10:
            logger.warn(f"  ... and {len(failed_venues) - 10} more")

    logger.separator("=", 70)
    logger.done(f"DISTRICT SHARD {shard_id} COMPLETE | Shows={len(detailed)} | Movies={len(final_summary)} | Success Rate={success_rate:.1f}%")
