"""
Daily mode: time cutoff and incremental merge with old data.

Two variants matching original behavior:
- Sync (bmsdaily1-8): simple minutes_left, updates ticketsSold/grossRevenue
- Async (bmsdaily9): post-midnight aware, updates sold/gross
"""
import json
import os
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))


def show_key(r):
    return (
        r.get("venue"),
        r.get("time"),
        r.get("session_id"),
        r.get("audi"),
    )


# =====================================================
# SYNC VARIANT (bmsdaily1-8)
# =====================================================
def minutes_left_sync(show_time_str):
    """
    Convert 'hh:mm AM/PM' to minutes left from now (IST).
    No post-midnight handling.
    """
    try:
        now = datetime.now(IST)
        t = datetime.strptime(show_time_str, "%I:%M %p")
        t = t.replace(
            year=now.year,
            month=now.month,
            day=now.day,
            tzinfo=IST,
        )
        return (t - now).total_seconds() / 60
    except Exception:
        return 9999


def merge_with_old_sync(fetched, detailed_file):
    """
    Merge fetched rows with old data (sync variant).
    Updates: totalSeats, available, ticketsSold, grossRevenue, minsLeft.
    Keeps disappeared shows forever.
    """
    if os.path.exists(detailed_file):
        with open(detailed_file, "r", encoding="utf-8") as f:
            old_rows = json.load(f)
    else:
        old_rows = []

    old_map = {show_key(r): r for r in old_rows}
    new_map = {}

    for r in fetched:
        key = show_key(r)
        if key in old_map:
            # Overwrite live fields only
            old_map[key].update({
                "totalSeats": r["totalSeats"],
                "available": r["available"],
                "ticketsSold": r["ticketsSold"],
                "grossRevenue": r["grossRevenue"],
                "minsLeft": r.get("minsLeft"),
            })
            new_map[key] = old_map[key]
        else:
            new_map[key] = r

    # Keep disappeared shows forever
    for key, r in old_map.items():
        if key not in new_map:
            new_map[key] = r

    return list(new_map.values())


# =====================================================
# ASYNC VARIANT (bmsdaily9)
# =====================================================
def merge_with_old_async(fetched, detailed_file):
    """
    Merge fetched rows with old data (async variant).
    Updates: totalSeats, available, sold, gross, minsLeft.
    Keeps disappeared shows forever.
    """
    if os.path.exists(detailed_file):
        with open(detailed_file, "r", encoding="utf-8") as f:
            old_rows = json.load(f)
    else:
        old_rows = []

    old_map = {show_key(r): r for r in old_rows}
    new_map = {}

    for r in fetched:
        key = show_key(r)
        if key in old_map:
            old_map[key].update({
                "totalSeats": r["totalSeats"],
                "available": r["available"],
                "sold": r["sold"],
                "gross": r["gross"],
                "minsLeft": r["minsLeft"],
            })
            new_map[key] = old_map[key]
        else:
            new_map[key] = r

    for key, r in old_map.items():
        if key not in new_map:
            new_map[key] = r

    return list(new_map.values())
