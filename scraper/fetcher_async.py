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
import json
import os
import re
import urllib.parse

from scraper.stealth import get_identity, reset_identity


def _load_local_env():
    """Load local District settings without overriding real environment vars."""
    env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
    if not os.path.isfile(env_path):
        return

    with open(env_path, encoding="utf-8") as env_file:
        for line in env_file:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip("\"'")
            if key:
                os.environ[key] = value


_load_local_env()

# ---- Optional Worker settings; direct District fetching is the default ----
WORKER_URL = os.environ.get("DISTRICT_WORKER_URL", "")
DISTRICT_UA = os.environ.get("DISTRICT_UA", "")
DISTRICT_KEY = os.environ.get("DISTRICT_KEY", "")


def _district_headers(logger=None):
    """
    Stealth headers (rotating UA, fake IP, District Origin/Referer) plus the
    worker's required auth headers. DISTRICT_UA, if set, overrides the
    rotated User-Agent so it still matches what the Worker expects.
    """
    identity = get_identity("district", logger)
    headers = identity.headers()
    if DISTRICT_UA:
        headers["User-Agent"] = DISTRICT_UA
    headers["x-api-key"] = DISTRICT_KEY
    return headers


def _direct_headers(logger=None):
    """Return browser headers for direct District page requests."""
    identity = get_identity("district", logger)
    headers = identity.headers()
    headers.update({
        "User-Agent": identity.ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": "https://www.district.in/",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
    })
    headers.pop("x-api-key", None)
    return headers

# Tunable via env var. District blocks bursts from high-concurrency clients.
_CONCURRENCY = int(os.environ.get("CONCURRENCY", "4"))


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
    """Build the direct District cinema page URL."""
    cid = venue.get("id") or venue.get("cinema_id") or ""
    slug = venue.get("slug") or _slugify(
        venue.get("district_name") or venue.get("name")
    )
    city = _slugify(_venue_city(venue))
    base = f"https://www.district.in/movies/{slug}-in-{city}-CD{cid}"
    return f"{base}?{urllib.parse.urlencode({'fromdate': date_district})}"


def _pick(value, keys):
    if not isinstance(value, dict):
        return None
    for key in keys:
        if value.get(key) is not None:
            return value[key]
    return None


def _parse_direct_page(html, page_url, cinema_id, city):
    """Convert District __NEXT_DATA__ into the existing worker payload shape."""
    match = re.search(
        r'<script id="__NEXT_DATA__"[^>]*>([\s\S]*?)</script>', html
    )
    if not match:
        return {"error": "no_next_data", "pageUrl": page_url, "pageData": {"sessions": []}}

    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return {"error": "json_parse_failed", "pageUrl": page_url, "pageData": {"sessions": []}}

    props = data.get("props", {}).get("pageProps", {})
    wrapped = props.get("data", {}).get("serverState")
    if isinstance(wrapped, dict):
        cine_root = (
            wrapped.get(str(cinema_id))
            or wrapped.get(cinema_id)
            or next(iter(wrapped.values()), wrapped)
        )
    else:
        cine_root = props or data
    if not isinstance(cine_root, dict):
        cine_root = {}

    meta = cine_root.get("meta") or {}
    cinema_data = meta.get("cinema") or {}
    cinema = {
        "name": cinema_data.get("name") or cinema_data.get("cinemaName") or "cinema",
        "address": cinema_data.get("address") or "",
        "chainKey": cinema_data.get("chainKey") or cinema_data.get("chain") or "",
    }

    movie_by_key = {}
    for movie in meta.get("movies") or []:
        info = {
            "contentId": movie.get("contentId", movie.get("id")),
            "movieId": movie.get("id"),
            "name": movie.get("name") or movie.get("label") or "Unknown",
            "label": movie.get("label") or movie.get("name") or "",
            "lang": movie.get("lang") or movie.get("language") or "",
            "scrnFmt": movie.get("scrnFmt") or "",
            "sndFmt": movie.get("sndFmt") or "",
            "censor": movie.get("censor") or "",
            "duration": movie.get("duration"),
            "genres": movie.get("grn") if isinstance(movie.get("grn"), list)
            else movie.get("genre") if isinstance(movie.get("genre"), list) else [],
            "poster": movie.get("appImgPath") or movie.get("imgPath") or "",
            "cover": movie.get("appCvrPath") or movie.get("cvrPath") or "",
            "thumbnail": movie.get("thumbnail") or "",
            "trailer": movie.get("trailer") if movie.get("trailer") != "NA" else "",
            "rating": movie.get("rnr"),
            "isNew": bool(movie.get("isNew")),
            "totalSessionCount": movie.get("totalSessionCount"),
        }
        if movie.get("id") is not None:
            movie_by_key[str(movie["id"])] = info
        if movie.get("contentId") is not None:
            movie_by_key[str(movie["contentId"])] = info

    movies = []
    sessions = []
    used = set()

    def append_session(session, mid):
        return {
            "mid": mid,
            "showTime": _pick(session, ["showTime", "showtime", "startTime"]),
            "lang": _pick(session, ["lang", "language"]),
            "scrnFmt": _pick(session, ["scrnFmt", "format", "screenFormat"]),
            "audi": _pick(session, ["audi", "audiName", "screen", "screenName"]),
            "sid": _pick(session, ["sid", "sessionId", "id"]),
            "areas": [
                {
                    "label": _pick(area, ["label", "name", "areaLabel"]),
                    "sTotal": _pick(area, ["sTotal", "seatsTotal", "total"]),
                    "sAvail": _pick(area, ["sAvail", "seatsAvail", "avail", "available"]),
                    "seatsTotal": _pick(area, ["seatsTotal", "sTotal", "total"]),
                    "seatsAvail": _pick(area, ["seatsAvail", "sAvail", "avail", "available"]),
                    "price": _pick(area, ["price", "priceInRs", "amount"]),
                }
                for area in (session.get("areas") or [])
            ],
        }

    for session in cine_root.get("pageData", {}).get("sessions") or []:
        info = movie_by_key.get(str(session.get("mid"))) or movie_by_key.get(str(session.get("contentId")))
        group_id = info.get("contentId") if info and info.get("contentId") is not None else session.get("mid")
        if str(group_id) not in used:
            used.add(str(group_id))
            name = (info or {}).get("name") or session.get("movieName") or "Unknown"
            lang = session.get("lang") or (info or {}).get("lang") or ""
            movies.append({**info, "id": group_id, "name": name, "lang": lang} if info else {"id": group_id, "name": name, "lang": lang})
        sessions.append(append_session(session, group_id))

    session_dates = cine_root.get("data", {}).get("sessionDates") or meta.get("showDates") or []
    session_dates = [str(value)[:10] for value in session_dates if value]
    return {
        "meta": {"cinema": cinema, "movies": movies},
        "pageData": {"sessions": sessions},
        "sessionDates": session_dates,
        "cinema_id": cinema_id,
        "city": city,
        "date": page_url.rsplit("fromdate=", 1)[-1],
    }


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
                if resp.status == 403 and retry_count < max_retries:
                    delay = (2 ** retry_count) + (asyncio.get_event_loop().time() % 1)
                    await asyncio.sleep(delay)
                    return await _fetch_one_with_retry(
                        session, venue, error_counts, failed_venues, logger,
                        date_district, max_retries, retry_count + 1
                    )
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
                    reset_identity(logger, site="district")
                    logger.warn(f"{cid} | District upstream blocked ({resp.status})")
                    failed_venues.append({"venue": cid, "error": f"District blocked ({resp.status})"})
                elif resp.status >= 500:
                    error_counts["server_error"] += 1
                    logger.error(f"{cid} | Server Error ({resp.status}) after {retry_count} retries")
                    failed_venues.append({"venue": cid, "error": f"Server Error ({resp.status})"})
                else:
                    error_counts["http_error"] += 1
                    logger.warn(f"{cid} | HTTP {resp.status}")
                    failed_venues.append({"venue": cid, "error": f"HTTP {resp.status}"})
                return None

            html = await resp.text()
            data = _parse_direct_page(
                html,
                url,
                venue.get("id") or venue.get("cinema_id") or "",
                _slugify(_venue_city(venue)),
            )

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
                    reset_identity(logger, site="district")
                    logger.warn(f"{cid} | District upstream blocked ({resp.status})")
                    failed_venues.append({"venue": cid, "error": f"District blocked ({resp.status})"})
                elif resp.status >= 500:
                    error_counts["server_error"] += 1
                    logger.error(f"{cid} | Server Error ({resp.status})")
                    failed_venues.append({"venue": cid, "error": f"Server Error ({resp.status})"})
                else:
                    error_counts["http_error"] += 1
                    logger.warn(f"{cid} | HTTP {resp.status}")
                    failed_venues.append({"venue": cid, "error": f"HTTP {resp.status}"})
                return None

            html = await resp.text()
            data = _parse_direct_page(
                html,
                url,
                venue.get("id") or venue.get("cinema_id") or "",
                _slugify(_venue_city(venue)),
            )

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

    async with aiohttp.ClientSession(
        timeout=settings["timeout"],
        headers=_direct_headers(logger),
    ) as session:

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
