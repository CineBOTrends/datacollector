"""
Unified shard combiner.
Replaces combine_shards.py, combine_dailyshards.py, combine_shards_rotate.py, simplecombiner.py
"""
import json
import os
import time
from datetime import datetime, timedelta
import pytz

from scraper.config import get_config

IST = pytz.timezone("Asia/Kolkata")


def load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except FileNotFoundError:
        print(f"\u26a0\ufe0f  MISSING: {path}")
        return []
    except json.JSONDecodeError as e:
        print(f"\u274c CORRUPT JSON: {path} - {e}")
        return []
    except Exception as e:
        print(f"\u274c ERROR reading {path}: {type(e).__name__}: {e}")
        return []


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def normalize_row(r, date_code):
    """Normalize field names across different shard sources."""
    r["movie"] = r.get("movie") or "Unknown"
    r["city"] = r.get("city") or "Unknown"
    r["state"] = r.get("state") or "Unknown"
    r["venue"] = r.get("venue") or "Unknown"
    r["address"] = r.get("address") or ""
    r["time"] = r.get("time") or ""
    r["audi"] = r.get("audi") or ""
    r["session_id"] = str(r.get("session_id") or "")
    r["chain"] = r.get("chain") or "Unknown"
    r["source"] = r.get("source") or "Unknown"
    r["date"] = r.get("date") or date_code

    r["totalSeats"] = int(r.get("totalSeats") or 0)
    r["available"] = int(r.get("available") or 0)
    # Support both old (sold/gross) and new (ticketsSold/grossRevenue) field names
    r["sold"] = int(r.get("ticketsSold") or r.get("sold") or 0)
    r["gross"] = float(r.get("grossRevenue") or r.get("gross") or 0.0)

    occ = r.get("occupancy", "")
    if isinstance(occ, (int, float)):
        r["occupancy"] = f"{round(float(occ), 2)}%"
    elif isinstance(occ, str):
        if not occ.endswith("%"):
            try:
                r["occupancy"] = f"{round(float(occ), 2)}%"
            except Exception:
                r["occupancy"] = "0%"
    else:
        r["occupancy"] = "0%"

    return r


def dedupe(rows):
    seen = set()
    out = []
    dupes = 0
    for r in rows:
        key = (
            r.get("venue", ""),
            r.get("time", ""),
            r.get("session_id", ""),
            r.get("audi", ""),
        )
        if key in seen:
            dupes += 1
            continue
        seen.add(key)
        out.append(r)
    return out, dupes


def combine_shards(mode: str, date_code: str = None, upload_r2: bool = True):
    """
    Combine all 9 shard outputs into final files.

    Args:
        mode: "advance", "daily", or "rotate"
        date_code: Optional YYYYMMDD override
        upload_r2: Whether to upload to R2 (default True)
    """
    config = get_config(mode, date_code)
    dc = config["date_code"]
    base_dir = config["base_dir"]

    now_ist = datetime.now(IST)
    last_updated = now_ist.strftime("%Y-%m-%d %H:%M IST")

    final_detailed = os.path.join(base_dir, "finaldetailed.json")
    final_summary = os.path.join(base_dir, "finalsummary.json")

    print(f"\U0001f4cc Using DATE_CODE: {dc}")
    print(f"\U0001f4c1 Using directory: {base_dir}")
    print(f"\u23f1 Last updated: {last_updated}")

    # Load all shards
    all_rows = []
    loaded_shards = 0
    missing_shards = []

    for i in range(1, 10):
        path = os.path.join(base_dir, f"detailed{i}.json")
        data = load_json(path)
        if data:
            print(f"\u2705 detailed{i}.json \u2192 {len(data)} rows")
            all_rows.extend(data)
            loaded_shards += 1
        else:
            missing_shards.append(i)

    print(f"\U0001f4ca Raw rows: {len(all_rows)}")
    print(f"\U0001f4e6 Loaded {loaded_shards}/9 shards")
    if missing_shards:
        print(f"\u26a0\ufe0f  Missing or empty shards: {missing_shards}")

    # Normalize all rows
    all_rows = [normalize_row(r, dc) for r in all_rows]

    # Dedupe (same-source exact repeats)
    final_rows, dupes = dedupe(all_rows)
    print(f"\U0001f9f9 Duplicates removed: {dupes}")

    # Dedupe across sources: the same theatre is listed under different names by
    # BMS and District, so the same show was being counted twice (inflating
    # shows / tickets / gross). Keep BMS (real per-seat prices), drop District's copy.
    from combiner.venue_map import cross_source_dedupe
    final_rows, cross = cross_source_dedupe(final_rows)
    print(f"\U0001f501 Cross-source duplicates removed (District/BMS): {cross}")
    print(f"\U0001f3af Final detailed rows: {len(final_rows)}")

    # Sort
    final_rows.sort(
        key=lambda x: (
            x["movie"],
            x["city"],
            x["venue"],
            x["time"],
        )
    )

    # Save finaldetailed.json
    save_json(final_detailed, {"last_updated": last_updated, "data": final_rows})
    print("\U0001f389 finaldetailed.json saved")

    # Build summary
    summary = {}
    for r in final_rows:
        movie = r["movie"]
        city = r["city"]
        state = r["state"]
        venue = r["venue"]
        chain = r["chain"]

        total = r["totalSeats"]
        sold = r["sold"]
        gross = r["gross"]
        occ = (sold / total * 100) if total else 0

        if movie not in summary:
            summary[movie] = {
                "shows": 0,
                "venues": set(),
                "cities": set(),
                "totalSeats": 0,
                "ticketsSold": 0,
                "grossRevenue": 0.0,
                "fastfilling": 0,
                "housefull": 0,
                "cityDetails": {},
            }

        m = summary[movie]
        m["shows"] += 1
        m["grossRevenue"] += gross
        m["ticketsSold"] += sold
        m["totalSeats"] += total
        m["venues"].add(venue)
        m["cities"].add(city)

        if occ >= 98:
            m["housefull"] += 1
        elif occ >= 50:
            m["fastfilling"] += 1

        ck = (city, state)
        if ck not in m["cityDetails"]:
            m["cityDetails"][ck] = {
                "city": city,
                "state": state,
                "venues": set(),
                "shows": 0,
                "totalSeats": 0,
                "ticketsSold": 0,
                "grossRevenue": 0.0,
                "fastfilling": 0,
                "housefull": 0,
            }

        d = m["cityDetails"][ck]
        d["venues"].add(venue)
        d["shows"] += 1
        d["grossRevenue"] += gross
        d["ticketsSold"] += sold
        d["totalSeats"] += total
        if occ >= 98:
            d["housefull"] += 1
        elif occ >= 50:
            d["fastfilling"] += 1

    # Finalize summary
    final_summary = {}
    for movie, m in summary.items():
        final_summary[movie] = {
            "shows": m["shows"],
            "venues": len(m["venues"]),
            "cities": len(m["cities"]),
            "totalSeats": m["totalSeats"],
            "ticketsSold": m["ticketsSold"],
            "occupancy": round((m["ticketsSold"] / m["totalSeats"]) * 100, 2) if m["totalSeats"] else 0.0,
            "grossRevenue": round(m["grossRevenue"], 2),
            "fastfilling": m["fastfilling"],
            "housefull": m["housefull"],
            "cityDetails": [],
        }

        for d in m["cityDetails"].values():
            final_summary[movie]["cityDetails"].append({
                "city": d["city"],
                "state": d["state"],
                "shows": d["shows"],
                "venues": len(d["venues"]),
                "totalSeats": d["totalSeats"],
                "ticketsSold": d["ticketsSold"],
                "occupancy": round((d["ticketsSold"] / d["totalSeats"]) * 100, 2) if d["totalSeats"] else 0.0,
                "grossRevenue": round(d["grossRevenue"], 2),
                "fastfilling": d["fastfilling"],
                "housefull": d["housefull"],
            })

    # Save finalsummary.json
    final_summary_file = os.path.join(base_dir, "finalsummary.json")
    save_json(
        final_summary_file,
        {"last_updated": last_updated, "movies": final_summary},
    )
    print("\U0001f389 finalsummary.json created successfully")
    print("\U0001f4c4 Files ready:")
    print(f"   \u2022 {final_detailed}")
    print(f"   \u2022 {final_summary_file}")

    # Upload to R2 if configured
    # Path format: {mode}/{year}/{month}/{day}.json
    if upload_r2:
        try:
            from services.r2_storage import upload_file, is_r2_configured

            if is_r2_configured():
                print("\nUploading to R2...")

                year, month, day = dc[:4], dc[4:6], dc[6:8]
                r2_mode = "daily" if mode == "daily" else "advance"
                r2_key = f"v1/{r2_mode}/{year}/{month}/{day}.json"

                upload_file(final_summary_file, r2_key)
                print("R2 upload complete!")

                print("Waiting 5s for R2 propagation...")
                time.sleep(5)
                print("R2 propagation delay complete")

                # Purge Cloudflare cache for the uploaded file
                try:
                    from services.hooks import purge_after_r2_upload
                    purge_after_r2_upload(r2_key)
                except ImportError:
                    pass
            else:
                print("\nR2 not configured, skipping cloud upload")
        except ImportError:
            print("\nR2 not configured, skipping cloud upload")
