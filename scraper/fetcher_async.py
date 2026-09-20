"""
Async fetcher using aiohttp (shard 9 - District CINEMA-page worker).

Talks to YOUR Cloudflare Worker, which scrapes one District cinema page and
returns all movies + sessions for that cinema. Requires User-Agent + x-api-key.

Mode-aware behavior:
- advance: retry with exponential backoff
- daily/rotate: no retries

Input: `venues` is your existing districtvenues.json list of cinemas. Each
entry needs an id; name + city are used to build the cinema page URL slug:
    {"id": 1088533, "name": "INOX M5 Ecity, Bengaluru", "City": "Bengaluru"}
"""
import asyncio
import aiohttp
import os
import re
import urllib.parse

# ---- Worker endpoint + auth (set as env vars; must match the Worker) ----
WORKER_URL = os.environ.get("DISTRICT_WORKER_URL", "")
DISTRICT_UA = os.environ.get("DISTRICT_UA", "")
DISTRICT_KEY = os.environ.get("DISTRICT_KEY", "")

HEADERS = {
    "User-Agent": DISTRICT_UA,
    "x-api-key": DISTRICT_KEY,
}

# Tunable via env var: CONCURRENCY (default 20)
_CONCURRENCY = int(os.environ.get("CONCURRENCY", "20"))


def _slugify(text):
    """'INOX M5 Ecity, Bengaluru' -> 'inox-m5-ecity-bengaluru'."""
    if not text:
        return "cinema"
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)   # non-alnum -> hyphen
    text = re.sub(r"-{2,}", "-", text).strip("-")
    return text or "cinema"


def _venue_city(venue):
    return venue.get("city") or venue.get("City") or ""


def _build_url(venue, date_district):
    """Build the worker URL for one cinema. The worker maps date -> fromdate."""
    cid = venue.get("id") or venue.get("cinema_id") or ""
    params = {
        "cinema_id": cid,
        "slug": venue.get("slug")
        or _slugify(venue.get("district_name") or venue.get("name")),
        "city": _slugify(_venue_city(venue)),
        "date": date_district,
    }
    return WORKER_URL.rstrip("/") + "/?" + urllib.parse.urlencode(params)


def _target_id(venue):
    cid = venue.get("id") or venue.get("cinema_id") or "?"
    return f"{cid}|{_venue_city(venue) or '?'}"


def get_async_settings(mode):
    """Get concurrency, timeout, and retry settings based on mode."""
    if mode == "advance":
        return {
            "concurrency": _CONCURRENCY,
            "timeout": aiohttp.ClientTimeout(total=25),
            "max_retries": 2,
            "has_retry": True,
        }
    else:  # daily, rotate
        return {
            "concurrency": _CONCURRENCY,
            "timeout": aiohttp.ClientTimeout(total=25),
            "max_retries": 0,
            "has_retry": False,
        }


def _ok_or_none(data, cid, error_counts, failed_venues, logger):
    """
    Validate a 200-status worker payload. The worker returns 200 even for soft
    errors (carrying an "error" key e.g. district_status_404 / no_next_data),
    so check that here. Returns True if there are usable sessions.
    """
    if data.get("error"):
        error_counts["http_error"] += 1
        logger.warn(f"{cid} | worker error: {data.get('error')}")
        failed_venues.append({"venue": cid, "error": str(data.get("error"))})
        return False

    sessions = data.get("pageData", {}).get("sessions") or []
    if not sessions:
        error_counts["no_shows"] += 1
        return False

    return True


# =====================================================
# FETCH ONE (WITH RETRY - advance mode)
# =====================================================
async def _fetch_one_with_retry(session, venue, error_counts, failed_venues, logger, date_district, max_retries, retry_count=0):
    cid = _target_id(venue)
    url = _build_url(venue, date_district)

    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                if resp.status >= 500 and retry_count < max_retries:
                    delay = (2 ** retry_count) + (asyncio.get_event_loop().time() % 1)
                    await asyncio.sleep(delay)
                    return await _fetch_one_with_retry(
                        session, venue, error_counts, failed_venues, logger,
                        date_district, max_retries, retry_count + 1
                    )

                if resp.status == 429:
                    error_counts["rate_limit"] += 1
                    logger.rate_limit(f"{cid} | Rate Limited (429)")
                    failed_venues.append({"venue": cid, "error": "Rate Limit (429)"})
                elif resp.status in (401, 403):
                    error_counts["blocked"] += 1
                    logger.warn(f"{cid} | Unauthorized/Forbidden ({resp.status}) - check DISTRICT_UA/DISTRICT_KEY")
                    failed_venues.append({"venue": cid, "error": f"Auth ({resp.status})"})
                elif resp.status >= 500:
                    error_counts["server_error"] += 1
                    logger.error(f"{cid} | Server Error ({resp.status}) after {retry_count} retries")
                    failed_venues.append({"venue": cid, "error": f"Server Error ({resp.status})"})
                else:
                    error_counts["http_error"] += 1
                    logger.warn(f"{cid} | HTTP {resp.status}")
                    failed_venues.append({"venue": cid, "error": f"HTTP {resp.status}"})
                return None

            data = await resp.json()

            if not _ok_or_none(data, cid, error_counts, failed_venues, logger):
                return None

            error_counts["success"] += 1
            return {"venue": venue, "data": data}

    except asyncio.TimeoutError:
        if retry_count < max_retries:
            delay = (2 ** retry_count) + (asyncio.get_event_loop().time() % 1)
            await asyncio.sleep(delay)
            return await _fetch_one_with_retry(
                session, venue, error_counts, failed_venues, logger,
                date_district, max_retries, retry_count + 1
            )
        error_counts["timeout"] += 1
        logger.error(f"{cid} | Timeout after {retry_count} retries")
        failed_venues.append({"venue": cid, "error": "Timeout"})
        return None
    except aiohttp.ClientError as e:
        if retry_count < max_retries:
            delay = (2 ** retry_count) + (asyncio.get_event_loop().time() % 1)
            await asyncio.sleep(delay)
            return await _fetch_one_with_retry(
                session, venue, error_counts, failed_venues, logger,
                date_district, max_retries, retry_count + 1
            )
        error_counts["network"] += 1
        logger.error(f"{cid} | Network Error: {type(e).__name__} after {retry_count} retries")
        failed_venues.append({"venue": cid, "error": f"Network ({type(e).__name__})"})
        return None
    except Exception as e:
        error_counts["other"] += 1
        logger.error(f"{cid} | {type(e).__name__}: {str(e)[:50]}")
        failed_venues.append({"venue": cid, "error": f"{type(e).__name__}"})
        return None


# =====================================================
# FETCH ONE (NO RETRY - daily/rotate mode)
# =====================================================
async def _fetch_one_no_retry(session, venue, error_counts, failed_venues, logger, date_district):
    cid = _target_id(venue)
    url = _build_url(venue, date_district)

    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                if resp.status == 429:
                    error_counts["rate_limit"] += 1
                    logger.rate_limit(f"{cid} | Rate Limited (429)")
                    failed_venues.append({"venue": cid, "error": "Rate Limit (429)"})
                elif resp.status in (401, 403):
                    error_counts["blocked"] += 1
                    logger.warn(f"{cid} | Unauthorized/Forbidden ({resp.status}) - check DISTRICT_UA/DISTRICT_KEY")
                    failed_venues.append({"venue": cid, "error": f"Auth ({resp.status})"})
                elif resp.status >= 500:
                    error_counts["server_error"] += 1
                    logger.error(f"{cid} | Server Error ({resp.status})")
                    failed_venues.append({"venue": cid, "error": f"Server Error ({resp.status})"})
                else:
                    error_counts["http_error"] += 1
                    logger.warn(f"{cid} | HTTP {resp.status}")
                    failed_venues.append({"venue": cid, "error": f"HTTP {resp.status}"})
                return None

            data = await resp.json()

            if not _ok_or_none(data, cid, error_counts, failed_venues, logger):
                return None

            error_counts["success"] += 1
            return {"venue": venue, "data": data}

    except asyncio.TimeoutError:
        error_counts["timeout"] += 1
        logger.error(f"{cid} | Timeout")
        failed_venues.append({"venue": cid, "error": "Timeout"})
        return None
    except aiohttp.ClientError as e:
        error_counts["network"] += 1
        logger.error(f"{cid} | Network Error: {type(e).__name__}")
        failed_venues.append({"venue": cid, "error": f"Network ({type(e).__name__})"})
        return None
    except Exception as e:
        error_counts["other"] += 1
        logger.error(f"{cid} | {type(e).__name__}: {str(e)[:50]}")
        failed_venues.append({"venue": cid, "error": f"{type(e).__name__}"})
        return None


# =====================================================
# FETCH ALL (ASYNC)
# =====================================================
async def fetch_all_async(venues, date_district, mode, logger):
    """
    Fetch all district cinemas asynchronously.

    Args:
        venues: list of cinema dicts (districtvenues.json) with at least "id"
                (plus "name"/"City" used to build the page slug)
        date_district: date in YYYY-MM-DD format
        mode: "advance", "daily", or "rotate"
        logger: logger instance

    Returns:
        (results, error_counts, failed_venues)
    """
    if not DISTRICT_KEY:
        logger.warn("DISTRICT_KEY env var is empty - worker will return 401. Set it to match the Worker's ALLOWED_KEY.")

    settings = get_async_settings(mode)
    sem = asyncio.Semaphore(settings["concurrency"])

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

    # districtvenues.json may be a dict keyed by id, or a list of dicts.
    if isinstance(venues, dict):
        venue_list = []
        for k, v in venues.items():
            v = dict(v)
            v.setdefault("id", k)
            venue_list.append(v)
        venues = venue_list

    async with aiohttp.ClientSession(timeout=settings["timeout"], headers=HEADERS) as session:

        async def bound(v):
            async with sem:
                if settings["has_retry"]:
                    return await _fetch_one_with_retry(
                        session, v, error_counts, failed_venues, logger,
                        date_district, settings["max_retries"]
                    )
                else:
                    return await _fetch_one_no_retry(
                        session, v, error_counts, failed_venues, logger,
                        date_district
                    )

        tasks = [bound(v) for v in venues]
        raw = await asyncio.gather(*tasks)

    results = [r for r in raw if r]
    logger.success(f"Fetched {len(results)} cinemas with shows")
    return results, error_counts, failed_venues
