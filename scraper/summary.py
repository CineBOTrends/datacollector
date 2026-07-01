"""
Summary builder - aggregates detailed show data into movie-level stats.

Mode-aware:
- advance/rotate: includes cityDetails with per-city breakdown, uses ticketsSold/grossRevenue
- daily sync: NO cityDetails, uses ticketsSold/grossRevenue
- daily async (shard 9): NO cityDetails, uses sold/gross field names
"""


def build_summary_with_city_details(detailed):
    """
    Build summary with cityDetails breakdown.
    Used by: advance sync (bms1-8), advance async (bms9), rotate sync, rotate async.
    Reads: ticketsSold, grossRevenue from rows.
    """
    summary = {}

    for r in detailed:
        movie = r["movie"]
        city = r["city"]
        state = r["state"]
        venue = r["venue"]

        total = r["totalSeats"]
        sold = r["ticketsSold"]
        gross = r["grossRevenue"]
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
                "movieInfo": r.get("movieInfo") or {},
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

    # Finalize
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
            "movieInfo": m.get("movieInfo", {}),
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

    return final_summary


def build_summary_daily_sync(detailed):
    """
    Build flat summary (NO cityDetails).
    Used by: bmsdaily1-8.
    Reads: ticketsSold, grossRevenue from rows.
    """
    summary = {}

    for r in detailed:
        movie = r["movie"]
        city = r["city"]
        venue = r["venue"]

        total = r["totalSeats"]
        sold = r["ticketsSold"]
        gross = r["grossRevenue"]
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
                "movieInfo": None,
            }

        if summary[movie]["movieInfo"] is None:
            summary[movie]["movieInfo"] = r.get("movieInfo") or {}

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

    return {
        movie: {
            "shows": m["shows"],
            "venues": len(m["venues"]),
            "cities": len(m["cities"]),
            "totalSeats": m["totalSeats"],
            "ticketsSold": m["ticketsSold"],
            "occupancy": round((m["ticketsSold"] / m["totalSeats"]) * 100, 2) if m["totalSeats"] else 0.0,
            "grossRevenue": round(m["grossRevenue"], 2),
            "fastfilling": m["fastfilling"],
            "housefull": m["housefull"],
            "movieInfo": m.get("movieInfo") or {},
        }
        for movie, m in summary.items()
    }


def build_summary_daily_async(detailed):
    """
    Build flat summary (NO cityDetails).
    Used by: bmsdaily9.
    Reads: sold, gross from rows (NOT ticketsSold/grossRevenue).
    """
    summary = {}

    for r in detailed:
        movie = r["movie"]
        city = r["city"]
        venue = r["venue"]

        total = r["totalSeats"]
        sold = r["sold"]
        gross = r["gross"]
        occ = (sold / total * 100) if total else 0

        if movie not in summary:
            summary[movie] = {
                "shows": 0,
                "gross": 0.0,
                "sold": 0,
                "totalSeats": 0,
                "venues": set(),
                "cities": set(),
                "fastfilling": 0,
                "housefull": 0,
                "movieInfo": None,
            }

        if summary[movie]["movieInfo"] is None:
            summary[movie]["movieInfo"] = r.get("movieInfo") or {}

        m = summary[movie]
        m["shows"] += 1
        m["gross"] += gross
        m["sold"] += sold
        m["totalSeats"] += total
        m["venues"].add(venue)
        m["cities"].add(city)

        if occ >= 98:
            m["housefull"] += 1
        elif occ >= 50:
            m["fastfilling"] += 1

    return {
        movie: {
            "shows": m["shows"],
            "gross": round(m["gross"], 2),
            "sold": m["sold"],
            "totalSeats": m["totalSeats"],
            "venues": len(m["venues"]),
            "cities": len(m["cities"]),
            "fastfilling": m["fastfilling"],
            "housefull": m["housefull"],
            "occupancy": round((m["sold"] / m["totalSeats"]) * 100, 2) if m["totalSeats"] else 0.0,
            "movieInfo": m.get("movieInfo") or {},
        }
        for movie, m in summary.items()
    }
