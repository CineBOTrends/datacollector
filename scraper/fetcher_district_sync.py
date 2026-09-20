"""
Synchronous fetcher for District (shard 9) — mirrors the exact stealth
approach fetcher_sync.py uses for BookMyShow:

- CloudScraper for Cloudflare/JS-challenge bypass
- Identity-based headers with fake IP (rotating UA, X-Forwarded-For)
- Hard timeout via threading (cross-platform)
- One request at a time, small randomized delay between requests
- Identity reset on any failed request

Only the target differs from BMS: District's cinema pages (HTML with an
embedded __NEXT_DATA__ blob), not a JSON API, so the response is parsed via
scraper.district_common instead of json().
"""
import random
import threading
import time

from scraper.district_stealth import get_district_identity, reset_district_identity
from scraper.district_common import _build_url, _parse_direct_page, _slugify, _target_id, _venue_city

# =====================================================
# CONFIG
# =====================================================
API_TIMEOUT = 20
HARD_TIMEOUT_SECONDS = 25


# =====================================================
# HARD TIMEOUT (threading-based, cross-platform)
# =====================================================
class TimeoutError(Exception):
    pass


def hard_timeout(seconds):
    def deco(fn):
        def wrapper(*args, **kwargs):
            result = [TimeoutError("Hard timeout hit")]

            def target():
                try:
                    result[0] = fn(*args, **kwargs)
                except Exception as e:
                    result[0] = e

            thread = threading.Thread(target=target)
            thread.daemon = True
            thread.start()
            thread.join(seconds)
            if thread.is_alive():
                raise TimeoutError("Hard timeout hit")
            if isinstance(result[0], Exception):
                raise result[0]
            return result[0]

        return wrapper

    return deco


# =====================================================
# FETCH ONE CINEMA PAGE
# =====================================================
@hard_timeout(HARD_TIMEOUT_SECONDS)
def _do_fetch(ident, url):
    """
    Actual HTTP call — runs inside timeout wrapper (may be daemon thread).
    Identity is passed explicitly so it works across thread boundaries.
    """
    r = ident.scraper.get(
        url, headers=ident.headers(), timeout=API_TIMEOUT, proxies=ident.proxy_dict()
    )

    if hasattr(r, "status_code") and r.status_code != 200:
        if r.status_code == 429:
            raise RuntimeError(f"RateLimit|{r.status_code}")
        elif r.status_code in (401, 403):
            raise RuntimeError(f"Blocked|{r.status_code}")
        elif r.status_code >= 500:
            raise RuntimeError(f"ServerError|{r.status_code}")
        else:
            raise RuntimeError(f"HTTPError|{r.status_code}")

    if "__NEXT_DATA__" not in r.text:
        raise RuntimeError("Blocked|HTML")

    return r.text


def fetch_cinema_page(venue, date_district, logger=None):
    """
    Public API. Gets identity in the CALLING thread (where thread_local
    persists across requests), then passes it to the timed fetch.

    Reusing the CloudScraper session/cookies across requests prevents bot
    detection from seeing rapid session churn — same reasoning as BMS.
    """
    ident = get_district_identity(logger)
    url = _build_url(venue, date_district)
    html = _do_fetch(ident, url)
    return url, html


# =====================================================
# FETCH ALL (SEQUENTIAL)
# =====================================================
def fetch_district_venues(venues, date_district, logger):
    """
    Fetch all District cinemas one at a time (same pacing/stealth model as
    BookMyShow's sync shards — no concurrency).

    Returns (results, error_counts, failed_venues) in the shape the rest of
    the pipeline (parser.py, summary.py) already expects.
    """
    if isinstance(venues, dict):
        venue_list = []
        for k, v in venues.items():
            v = dict(v)
            v.setdefault("id", k)
            venue_list.append(v)
        venues = venue_list

    error_counts = {
        "rate_limit": 0,
        "blocked": 0,
        "http_error": 0,
        "server_error": 0,
        "timeout": 0,
        "network": 0,
        "other": 0,
        "no_shows": 0,
        "success": 0,
    }
    failed_venues = []
    results = []

    total_venues = len(venues)
    for i, venue in enumerate(venues, 1):
        if i == 1 or i == total_venues or i % 50 == 0:
            logger.progress(f"[{i}/{total_venues}] Processing venues...")

        cid = _target_id(venue)
        try:
            url, html = fetch_cinema_page(venue, date_district, logger)
            data = _parse_direct_page(
                html, url, venue.get("id") or venue.get("cinema_id") or "",
                _slugify(_venue_city(venue)),
            )

            if data.get("error"):
                error_counts["http_error"] += 1
                logger.warn(f"{cid} | {data.get('error')}")
                failed_venues.append({"venue": cid, "error": str(data.get("error"))})
                continue

            sessions = data.get("pageData", {}).get("sessions") or []
            if not sessions:
                error_counts["no_shows"] += 1
                continue

            error_counts["success"] += 1
            results.append({"venue": venue, "data": data})

        except Exception as e:
            reset_district_identity(logger)
            error_msg = str(e)
            error_type = type(e).__name__

            if "RateLimit" in error_msg or "429" in error_msg:
                error_counts["rate_limit"] += 1
                logger.rate_limit(f"{cid} | Rate Limited (429)")
                failed_venues.append({"venue": cid, "error": "Rate Limit (429)"})
            elif "Blocked" in error_msg or "403" in error_msg or "HTML" in error_msg:
                error_counts["blocked"] += 1
                logger.warn(f"{cid} | District upstream blocked: {error_msg}")
                failed_venues.append({"venue": cid, "error": f"District blocked ({error_msg})"})
            elif "ServerError" in error_msg:
                error_counts["server_error"] += 1
                logger.error(f"{cid} | Server Error: {error_msg}")
                failed_venues.append({"venue": cid, "error": f"Server Error ({error_msg})"})
            elif "TimeoutError" in error_type or "timeout" in error_msg.lower():
                error_counts["timeout"] += 1
                logger.error(f"{cid} | Timeout: {error_type}")
                failed_venues.append({"venue": cid, "error": f"Timeout ({error_type})"})
            elif "HTTPError" in error_msg:
                error_counts["http_error"] += 1
                logger.warn(f"{cid} | HTTP Error: {error_msg}")
                failed_venues.append({"venue": cid, "error": f"HTTP Error ({error_msg})"})
            elif "ConnectionError" in error_type:
                error_counts["network"] += 1
                logger.error(f"{cid} | Network: {error_type} - {error_msg}")
                failed_venues.append({"venue": cid, "error": f"Network ({error_type})"})
            else:
                error_counts["other"] += 1
                logger.error(f"{cid} | {error_type}: {error_msg}")
                failed_venues.append({"venue": cid, "error": f"{error_type}: {error_msg}"})

        time.sleep(random.uniform(0.35, 0.7))

    logger.success(f"Fetched {len(results)} cinemas with shows")
    return results, error_counts, failed_venues
