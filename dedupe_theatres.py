#!/usr/bin/env python3
"""
Cross-source theatre de-duplication.
=====================================
The same physical cinema is often scraped by BOTH BMS and District, and each
source names/formats it differently:
    "PVR Lido, Santacruz (W), Mumbai"   vs   "PVR: Lido, Juhu Mumbai"
    "MOVIE TIME: HUB, Goregaon (E)"     vs   "MovieTime Hub Mall, Goregaon (E), Mumbai"
Left as-is, these show up as TWO separate theatre cards, and — worse — both
get folded into the city/state/movie/territory totals, silently doubling the
real gross/sold for that venue.

This groups rows per (city, raw venue), then clusters groups that are almost
certainly the same real theatre (same chain, same city, heavy address/name
token overlap) and keeps only the most complete group per cluster instead of
summing them.

Shared by build_data.py (per-movie kpi/state/city/format + the embedded
per-movie "territory"/All-India breakdown) and territory_report.py (the
standalone all-movies admin territory report), so every number the site
publishes — Today's Breakdown, State/City/Format Wise, and the All India
Report tab — is built from exactly the same de-duplicated row set and can
never drift apart from each other.
"""
import re
from collections import defaultdict

_VENUE_STOPWORDS = {
    "the", "near", "opp", "opposite", "road", "rd", "street", "st", "complex",
    "mall", "malls", "multiplex", "multiplexes", "cinema", "cinemas",
    "megaplex", "mumbai", "maharashtra", "india", "floor", "1st", "2nd",
    "3rd", "4th", "5th", "ave", "avenue", "compound", "junction", "station",
    "metro", "market", "city", "and", "of", "in", "at",
}


def _venue_tokens(*texts):
    toks = set()
    for text in texts:
        t = re.sub(r"[^a-z0-9\s]", " ", (text or "").lower())
        toks |= {w for w in t.split() if len(w) >= 3 and w not in _VENUE_STOPWORDS}
    return toks


def _venue_chain_key(chain, venue):
    c = (chain or "").strip()
    if not c:
        c = re.split(r"[:\-,\u00b7]", venue or "")[0]     # \u00b7 = '·'
    key = re.sub(r"[^a-z0-9]", "", c.lower())
    # PVR and INOX merged into PVR INOX Ltd in 2023. Sources inconsistently
    # tag the SAME physical theatre with either brand name (e.g. one row's
    # `chain` is "INOX", another row for the identical venue has `chain`
    # "Pvr"). Without this, those rows get different chain_keys, the cluster
    # check below bails out before ever comparing address/name tokens, and
    # the same theatre shows up as two cards with box office split across
    # both. Collapse both brands to one key so the token-overlap check can
    # still tell genuinely different venues apart.
    if "pvr" in key or "inox" in key:
        return "pvrinox"
    return key


def _row_sold(r):
    """Tolerant of both collector schemas (ticketsSold/sold)."""
    sold = r.get("ticketsSold")
    if sold is None:
        sold = r.get("sold")
    try:
        return int(sold or 0)
    except (TypeError, ValueError):
        return 0


def dedupe_theatre_rows(rows):
    """Collapse rows for the SAME physical theatre reported under different
    venue-name strings by different sources. Returns a filtered row list
    where each real theatre contributes only once, so downstream gross/sold
    aggregation (kpi, state/city/format, and the territory/All-India report)
    is never double-counted.

    Call this ONCE per movie's row group (or once over the whole day's rows
    for the admin all-movies report), before handing rows to any aggregator.
    """
    rows_by_city_venue = defaultdict(list)
    for r in rows:
        city = (r.get("city") or "Unknown").strip() or "Unknown"
        venue = (r.get("venue") or "Unknown").strip() or "Unknown"
        rows_by_city_venue[(city, venue)].append(r)

    groups = []
    for (city, venue), grp_rows in rows_by_city_venue.items():
        chain = next((r.get("chain") for r in grp_rows if r.get("chain")), "")
        address = next((r.get("address") for r in grp_rows if r.get("address")), "")
        sources = {r.get("source") for r in grp_rows if r.get("source")}
        groups.append({
            "city": city, "venue": venue, "chain": chain, "address": address,
            "chain_key": _venue_chain_key(chain, venue),
            "tokens": _venue_tokens(venue, address),
            # Single source if every row in this venue group agrees, else
            # None (mixed/unknown) — see the source check in the clustering
            # loop below for why this matters.
            "source": next(iter(sources)) if len(sources) == 1 else None,
            "rows": grp_rows,
        })

    merged_rows = []
    used = [False] * len(groups)
    for i, g in enumerate(groups):
        if used[i]:
            continue
        cluster = [g]
        used[i] = True
        for j in range(i + 1, len(groups)):
            if used[j]:
                continue
            h = groups[j]
            if h["city"] != g["city"] or h["chain_key"] != g["chain_key"]:
                continue
            # This function exists ONLY to catch the same physical theatre
            # being scraped by BOTH BMS and District under two different
            # name strings. It must never compare two rows from the SAME
            # source — each source's own venue master list already lists
            # every real theatre exactly once, so two BMS (or two District)
            # entries are, by definition, different theatres, however much
            # their name/address text overlaps. Without this check, a local
            # chain that owns several distinct single-screen theatres on the
            # same road in one town (e.g. "Pratap Group Theaters" running
            # Pratap Theater, Pratap Delux, KrishnaTeja and Srinivas Teja,
            # all on Jayasam Road, Tirupati) gets its separate theatres
            # wrongly collapsed into one, silently dropping real venues.
            if not g["source"] or not h["source"] or g["source"] == h["source"]:
                continue
            if not g["tokens"] or not h["tokens"]:
                continue
            overlap = g["tokens"] & h["tokens"]
            jaccard = len(overlap) / len(g["tokens"] | h["tokens"])
            if jaccard >= 0.34 and len(overlap) >= 2:
                cluster.append(h)
                used[j] = True
        if len(cluster) == 1:
            merged_rows.extend(g["rows"])
            continue
        # True duplicate across sources: keep the most complete-looking
        # group (most shows, then most tickets sold, then longest/most
        # descriptive address) instead of summing — summing would double
        # count real box office that both sources scraped independently.
        winner = max(cluster, key=lambda c: (
            len(c["rows"]),
            sum(_row_sold(r) for r in c["rows"]),
            len(c["address"] or ""),
        ))
        merged_rows.extend(winner["rows"])
    return merged_rows