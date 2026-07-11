#!/usr/bin/env python3
"""
Cron scheduler — reads schedule_config.yaml and runs scraper jobs at precise IST times.
Runs as a long-lived process (suitable for Docker CMD).
"""
import os
import sys
import signal
import time as _time
import yaml
import pytz
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from services.logger import get_logger

logger = get_logger(shard_id=None, log_file=None)

DEFAULT_CONFIG = os.path.join(PROJECT_ROOT, "schedule_config.yaml")


def load_config(config_path):
    with open(config_path, "r") as f:
        return yaml.safe_load(f)


def run_advance_job():
    """Triggered by scheduler for advance mode."""
    import time
    logger.separator("=")
    logger.start("SCHEDULED: Advance scrape triggered")
    logger.separator("=")
    start = time.time()
    try:
        from runner import run_advance
        run_advance()
        logger.success(f"Advance job completed in {(time.time() - start) / 60:.1f} min")
        publish("advance")
    except Exception as e:
        logger.error(f"Advance job failed after {(time.time() - start) / 60:.1f} min: {e}")
        import traceback
        traceback.print_exc()


def run_daily_job():
    """Triggered by scheduler for daily mode."""
    import time
    logger.separator("=")
    logger.start("SCHEDULED: Daily scrape triggered")
    logger.separator("=")
    start = time.time()
    try:
        from runner import run_daily
        run_daily()
        logger.success(f"Daily job completed in {(time.time() - start) / 60:.1f} min")
        publish("daily")
    except Exception as e:
        logger.error(f"Daily job failed after {(time.time() - start) / 60:.1f} min: {e}")
        import traceback
        traceback.print_exc()


def publish(mode):
    """After a successful scrape: build the data/ tree and push it to the
    dashboard repo so Cloudflare Pages (cinebotrends.com) rebuilds."""
    import subprocess
    import shutil
    logger.separator("-")
    logger.start(f"PUBLISH: build data tree + push to dashboard ({mode})")

    # 0) back up this run's raw data to R2 (no-op if R2 not configured)
    try:
        import ci_persist
        ci_persist.save(mode)
    except Exception as e:
        logger.error(f"R2 save skipped: {e}")

    # 1) build data/ (+ data-public/) from the collector's raw outputs
    try:
        import build_data
        build_data.main(PROJECT_ROOT)
    except Exception as e:
        logger.error(f"build_data failed: {e}")
        return

    # 2) push data/ to the dashboard repo
    cfg = load_config(DEFAULT_CONFIG).get("publish", {})
    repo = cfg.get("dashboard_repo", "https://github.com/CineBOTrends/dashboard.git")
    pubdir = os.path.abspath(
        os.path.join(PROJECT_ROOT, cfg.get("dashboard_dir", "../dashboard-publish"))
    )
    data_src = os.path.join(PROJECT_ROOT, "data")
    try:
        if not os.path.isdir(os.path.join(pubdir, ".git")):
            subprocess.run(["git", "clone", repo, pubdir], check=True)
        subprocess.run(["git", "-C", pubdir, "pull", "--quiet"], check=False)

        dst = os.path.join(pubdir, "data")
        if os.path.isdir(dst):
            shutil.rmtree(dst)
        shutil.copytree(data_src, dst)

        subprocess.run(["git", "-C", pubdir, "add", "-f", "data"], check=True)
        stamp = datetime.now().isoformat(timespec="seconds")
        r = subprocess.run(
            ["git", "-C", pubdir, "commit", "-m", f"data({mode}): {stamp}"]
        )
        if r.returncode == 0:
            subprocess.run(["git", "-C", pubdir, "push"], check=True)
            logger.success("Published -> dashboard (Cloudflare will rebuild)")
        else:
            logger.info("No data changes to publish")
    except Exception as e:
        logger.error(f"publish/push failed: {e}")


def run_cleanup_job(retain_days):
    """Triggered by scheduler for data cleanup."""
    logger.separator("=")
    logger.start(f"SCHEDULED: Cleanup triggered (retain {retain_days} days)")
    logger.separator("=")
    try:
        from services.cleanup import cleanup_old_data
        deleted = cleanup_old_data(retain_days=retain_days)
        logger.success(f"Cleanup completed. Folders removed: {deleted}")
    except Exception as e:
        logger.error(f"Cleanup failed: {e}")
        import traceback
        traceback.print_exc()


def start_scheduler(config_path=None):
    """Load config and start the blocking scheduler."""
    config_path = config_path or DEFAULT_CONFIG
    config = load_config(config_path)

    tz_name = config.get("timezone", "Asia/Kolkata")
    tz = pytz.timezone(tz_name)

    scheduler = BackgroundScheduler(timezone=tz)

    # Schedule advance jobs
    advance_times = config.get("advance", [])
    for time_str in advance_times:
        hour, minute = time_str.strip().split(":")
        scheduler.add_job(
            run_advance_job,
            CronTrigger(hour=int(hour), minute=int(minute), timezone=tz),
            id=f"advance_{time_str}",
            name=f"Advance @ {time_str} IST",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=300,
        )

    # Schedule daily jobs
    daily_times = config.get("daily", [])
    for time_str in daily_times:
        hour, minute = time_str.strip().split(":")
        scheduler.add_job(
            run_daily_job,
            CronTrigger(hour=int(hour), minute=int(minute), timezone=tz),
            id=f"daily_{time_str}",
            name=f"Daily @ {time_str} IST",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=300,
        )

    # Schedule cleanup job
    cleanup_config = config.get("cleanup", {})
    cleanup_time = cleanup_config.get("time")
    retain_days = cleanup_config.get("retain_days", 10)
    if cleanup_time:
        hour, minute = cleanup_time.strip().split(":")
        scheduler.add_job(
            run_cleanup_job,
            CronTrigger(hour=int(hour), minute=int(minute), timezone=tz),
            args=[retain_days],
            id="cleanup",
            name=f"Cleanup @ {cleanup_time} IST (retain {retain_days}d)",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=300,
        )

    # Print schedule summary
    logger.separator("=")
    logger.info(f"SCHEDULER STARTED | Timezone: {tz_name}")
    logger.info(f"Config: {config_path}")
    logger.separator("-")

    logger.info(f"Advance jobs ({len(advance_times)}):")
    for t in advance_times:
        logger.info(f"  {t} IST")

    logger.info(f"Daily jobs ({len(daily_times)}):")
    for t in daily_times:
        logger.info(f"  {t} IST")

    if cleanup_time:
        logger.info(f"Cleanup: {cleanup_time} IST (retain {retain_days} days)")

    logger.separator("-")

    # Show next few upcoming jobs
    jobs = scheduler.get_jobs()
    upcoming = sorted(jobs, key=lambda j: j.trigger.get_next_fire_time(None, datetime.now(tz)))
    logger.info("Next 5 upcoming jobs:")
    for job in upcoming[:5]:
        nxt = job.trigger.get_next_fire_time(None, datetime.now(tz))
        logger.info(f"  {job.name} -> {nxt.strftime('%Y-%m-%d %H:%M %Z')}")

    logger.separator("=")
    logger.info("Scheduler running. Press Ctrl+C to stop.")

    # Graceful shutdown
    def shutdown(signum, frame):
        logger.info("Shutting down scheduler...")
        scheduler.shutdown(wait=False)
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    scheduler.start()

    # Keep main thread alive
    while True:
        _time.sleep(60)


if __name__ == "__main__":
    start_scheduler()
