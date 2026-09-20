"""
District URL-building and __NEXT_DATA__ parsing helpers.

Shared by the synchronous District fetcher (fetcher_district_sync.py) and any
diagnostic scripts (test_district.py) — kept independent of BookMyShow code.
"""
import json
import re
import urllib.parse


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


def _target_id(venue):
    cid = venue.get("id") or venue.get("cinema_id") or "?"
    return f"{cid}|{_venue_city(venue) or '?'}"


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
