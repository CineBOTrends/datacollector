"""
Configuration module for BMS scraper.
Handles date calculation, paths, and environment detection.
"""
import os
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))

# Base directory for the project (where venues/ folder lives)
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def get_config(mode: str, date_code: str = None) -> dict:
    """
    Build configuration based on scrape mode.

    Args:
        mode: "advance", "daily", or "rotate"
        date_code: Optional YYYYMMDD date override

    Returns:
        dict with: date_code, date_district, base_dir, is_daily, cutoff_minutes
    """
    if mode == "advance":
        if not date_code:
            date_code = os.environ.get("DATE_CODE") or (
                datetime.now(IST) + timedelta(days=1)
            ).strftime("%Y%m%d")
    elif mode == "daily":
        date_code = datetime.now(IST).strftime("%Y%m%d")
    elif mode == "rotate":
        if not date_code:
            date_code = os.environ.get("DATE_CODE")
        if not date_code:
            raise ValueError(
                "DATE_CODE is required for rotate mode. "
                "Set it via --date flag or DATE_CODE environment variable."
            )
    else:
        raise ValueError(f"Unknown mode: {mode}. Use 'advance', 'daily', or 'rotate'.")

    # District API uses YYYY-MM-DD format
    date_district = f"{date_code[:4]}-{date_code[4:6]}-{date_code[6:8]}"

    # Base directory for output
    if mode == "daily":
        base_dir = os.path.join("daily", "data", date_code)
    else:
        base_dir = os.path.join("advance", "data", date_code)

    # Cutoff only applies to daily mode: drops shows starting more than
    # cutoff_minutes from now, so each daily run only captures near-term
    # showtimes. Override with DAILY_CUTOFF_MINUTES=off (or "none"/"0") to
    # capture the whole day's showtimes in a single run instead, or set it
    # to a specific number of minutes for a custom window.
    if mode == "daily":
        raw_cutoff = os.environ.get("DAILY_CUTOFF_MINUTES")
        if raw_cutoff is None:
            cutoff_minutes = 75          # default: near-term shows only
        elif raw_cutoff.strip().lower() in ("off", "none", "0", ""):
            cutoff_minutes = None        # no filter: capture the whole day
        else:
            cutoff_minutes = int(raw_cutoff)
    else:
        cutoff_minutes = None

    return {
        "mode": mode,
        "date_code": date_code,
        "date_district": date_district,
        "base_dir": base_dir,
        "is_daily": mode == "daily",
        "cutoff_minutes": cutoff_minutes,
        "project_root": PROJECT_ROOT,
    }