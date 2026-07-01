"""Cleanup old data folders (advance/data/YYYYMMDD, daily/data/YYYYMMDD)."""
import os
import shutil
from datetime import datetime, timedelta
import pytz

IST = pytz.timezone("Asia/Kolkata")
BASE_PATHS = ["advance/data", "daily/data"]


def cleanup_old_data(retain_days=10):
    """
    Delete entire date folders older than retain_days.

    Scans advance/data/ and daily/data/ for YYYYMMDD folders.
    """
    cutoff = (datetime.now(IST) - timedelta(days=retain_days)).strftime("%Y%m%d")
    deleted = 0

    print(f"Cleanup: removing folders older than {retain_days} days (before {cutoff})")

    for base in BASE_PATHS:
        if not os.path.isdir(base):
            continue
        for folder_name in sorted(os.listdir(base)):
            if not folder_name.isdigit() or len(folder_name) != 8:
                continue
            if folder_name < cutoff:
                folder_path = os.path.join(base, folder_name)
                shutil.rmtree(folder_path)
                print(f"Deleted: {folder_path}")
                deleted += 1

    print(f"Cleanup complete. Folders removed: {deleted}")
    return deleted


if __name__ == "__main__":
    cleanup_old_data()
