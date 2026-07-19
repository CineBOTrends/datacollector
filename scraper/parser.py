"""
Parsers for BMS API and District API responses.

- parse_bms: for shards 1-8 (BMS venue API)
- parse_district_*: for shard 9 (District CINEMA-page worker)

District parsers consume the cinema-page worker output (ONE cinema, MANY
movies):
    {
      "meta": { "cinema": {name, address, chainKey},
                "movies": [{id, name, lang}] },
      "pageData": { "sessions": [{mid, showTime, lang, scrnFmt, audi, sid, areas}] },
      "sessionDates": [...],
    }
Each `res` in `results` is one worker response: res["data"] = worker JSON,
res["venue"] = the cinema's row from districtvenues.json (id/city/state/...),
used for city/state and as a fallback for the cinema name.
"""
from datetime import datetime, timedelta
import pytz

IST_TZ = pytz.timezone("Asia/Kolkata")


# =====================================================
# HELPERS (District API)
# =====================================================
# 56% of District venue records (1431/2518) carry no state at all — they are
# stubs with just a city and an id, no address or pincode either. Those rows all
# collapsed into a single bogus "Unknown" state on the dashboard, even though
# their cities (Mumbai, New Delhi, Ahmedabad...) obviously belong somewhere.
# venues/city_state.json maps city -> state and fills the gap.
_CITY_STATE = None


def _city_state_map():
    global _CITY_STATE
    if _CITY_STATE is None:
        import json as _json
        import os as _os
        fp = _os.path.join(
            _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
            "venues", "city_state.json",
        )
        try:
            with open(fp, encoding="utf-8") as f:
                _CITY_STATE = _json.load(f)
        except Exception:
            _CITY_STATE = {}
    return _CITY_STATE


def format_state(s, city=None):
    """Normalise the state. Falls back to a city lookup when it's missing."""
    if not s:
        if city:
            hit = _city_state_map().get(" ".join(str(city).split()).casefold())
            if hit:
                return hit
        return "Unknown"
    return " ".join(w.capitalize() for w in s.replace("-", " ").split())


def format_chain(s):
    if not s:
        return "Unknown"
    return " ".join(w.capitalize() for w in s.replace("-", " ").split())


def _fmt_time(show_time):
    """
    Convert a District showTime ('YYYY-MM-DDTHH:MM[:SS][+offset]') to an
    IST 'hh:MM AM/PM' string. The naive timestamp is treated as UTC. Returns
    "" on bad/empty input.
    """
    if not show_time:
        return ""
    try:
        return (
            datetime.strptime(str(show_time)[:16], "%Y-%m-%dT%H:%M")
            .replace(tzinfo=pytz.UTC)
            .astimezone(IST_TZ)
            .strftime("%I:%M %p")
        )
    except (ValueError, TypeError):
        return ""


def _district_session_stats(session):
    """
    Aggregate seat counts and gross from areas[].
    Falls back to session-level total/avail only when areas[] is absent.
    """
    areas = session.get("areas", []) or []
    if areas:
        total = sum(int(a.get("sTotal") or a.get("seatsTotal") or 0) for a in areas)
        avail = sum(int(a.get("sAvail") or a.get("seatsAvail") or 0) for a in areas)
        gross = sum(
            (int(a.get("sTotal") or a.get("seatsTotal") or 0)
             - int(a.get("sAvail") or a.get("seatsAvail") or 0))
            * float(a.get("price") or 0)
            for a in areas
        )
    else:
        total = int(session.get("total") or 0)
        avail = int(session.get("avail") or 0)
        gross = 0.0
    return total, avail, total - avail, gross


def _movie_label(name, scrn_fmt, lang):
    dim = scrn_fmt or ""
    lang = lang or ""
    suffix = " - ".join(x for x in (dim, lang) if x)
    return f"{name} ({suffix})" if suffix else name


def _movie_info(movie):
    """Full movie-card details carried from the District worker's meta.movies."""
    if not movie:
        return {}
    info = {
        "contentId": movie.get("contentId") or movie.get("id"),
        "name": movie.get("name"),
        "lang": movie.get("lang"),
        "scrnFmt": movie.get("scrnFmt"),
        "sndFmt": movie.get("sndFmt"),
        "censor": movie.get("censor"),
        "duration": movie.get("duration"),
        "genres": movie.get("genres"),
        "poster": movie.get("poster"),
        "cover": movie.get("cover"),
        "thumbnail": movie.get("thumbnail"),
        "trailer": movie.get("trailer"),
        "rating": movie.get("rating"),
        "isNew": movie.get("isNew"),
        "totalSessionCount": movie.get("totalSessionCount"),
    }

    # Release date: this whitelist never captured it, so movieInfo has always
    # arrived without one — which is why build_data's meta.releaseDate is null
    # and why upcoming-release detection found nothing. District spells the key
    # differently across payloads, so pass through anything release-ish rather
    # than guessing a single name, and normalise it to "releaseDate".
    for k, v in movie.items():
        if v in (None, "", 0):
            continue
        kl = k.lower()
        if "releas" in kl or kl in ("rd", "opendate", "openingdate"):
            info.setdefault("releaseDate", v)
            info[k] = v                      # keep the original key too

    return info


# =====================================================
# BMS API PARSER (shards 1-8)  -- unchanged
# =====================================================
def parse_bms(data, date_code):
    """
    Parse BMS venue API response.

    Output fields: movie, venue, address, chain, time, audi, session_id,
                   totalSeats, available, ticketsSold, grossRevenue
    """
    out = []

    sd = data.get("ShowDetails", [])
    if not sd:
        return out

    venue = sd[0].get("Venues", {})
    venue_name = venue.get("VenueName", "")
    venue_add = venue.get("VenueAdd", "")
    chain = venue.get("VenueCompName", "Unknown")

    for ev in sd[0].get("Event", []):
        title = ev.get("EventTitle", "Unknown")

        for ch in ev.get("ChildEvents", []):
            dim = ch.get("EventDimension", "").strip()
            lang = ch.get("EventLanguage", "").strip()
            suffix = " - ".join(x for x in (dim, lang) if x)
            movie = f"{title} ({suffix})" if suffix else title

            for sh in ch.get("ShowTimes", []):
                show_date = sh.get("ShowDateCode")
                if show_date != date_code:
                    continue

                total = sold = avail = gross = 0
                for cat in sh.get("Categories", []):
                    seats = int(cat.get("MaxSeats", 0))
                    free = int(cat.get("SeatsAvail", 0))
                    price = float(cat.get("CurPrice", 0))
                    total += seats
                    avail += free
                    sold += seats - free
                    gross += (seats - free) * price

                out.append({
                    "movie": movie,
                    "venue": venue_name,
                    "address": venue_add,
                    "chain": chain,
                    "time": sh.get("ShowTime", ""),
                    "audi": sh.get("Attributes", "") or "",
                    "session_id": str(sh.get("SessionId", "")),
                    "totalSeats": total,
                    "available": avail,
                    "ticketsSold": sold,
                    "grossRevenue": round(gross, 2),
                })

    return out


# =====================================================
# DISTRICT CINEMA PARSER - ADVANCE/ROTATE mode
# =====================================================
def parse_district_advance(results, date_code):
    """
    Output fields: movie, city, state, venue, address, time, audi, session_id,
                   totalSeats, available, ticketsSold, grossRevenue,
                   occupancy ("X%"), source, date, chain
    """
    from scraper.dedupe import dedupe

    detailed = []

    for res in results:
        venue_meta = res.get("venue", {}) or {}
        data = res.get("data", {}) or {}

        city = venue_meta.get("city") or venue_meta.get("City") or "Unknown"
        state = format_state(
            venue_meta.get("state") or venue_meta.get("State"), city
        )

        cinema = data.get("meta", {}).get("cinema", {}) or {}
        venue_name = cinema.get("name") or venue_meta.get("name") or venue_meta.get("district_name") or "Unknown"
        venue_addr = cinema.get("address") or venue_meta.get("address") or ""
        chain = format_chain(
            cinema.get("chainKey") or venue_meta.get("chainKey")
            or venue_meta.get("chain") or venue_name
        )

        movies = data.get("meta", {}).get("movies", []) or []
        movie_map = {}
        for m in movies:
            movie_map[m.get("id")] = m
            movie_map[str(m.get("id"))] = m

        for s in data.get("pageData", {}).get("sessions", []) or []:
            mid = s.get("mid")
            movie = movie_map.get(mid) or movie_map.get(str(mid))
            if not movie:
                continue

            name = movie.get("name", "Unknown")
            lang = s.get("lang") or movie.get("lang") or ""
            movie_key = _movie_label(name, s.get("scrnFmt"), lang)

            total, avail, sold, gross = _district_session_stats(s)
            occ = (sold / total * 100) if total else 0

            detailed.append({
                "movie": movie_key,
                "city": city,
                "state": state,
                "venue": venue_name,
                "address": venue_addr,
                "time": _fmt_time(s.get("showTime")),
                "audi": s.get("audi", "") or "",
                "session_id": str(s.get("sid") or s.get("mid") or ""),
                "totalSeats": total,
                "available": avail,
                "ticketsSold": sold,
                "grossRevenue": round(gross, 2),
                "occupancy": f"{round(occ, 2)}%",
                "source": "District",
                "date": date_code,
                "chain": chain,
                "movieInfo": _movie_info(movie),
            })

    return dedupe(detailed)


# =====================================================
# DISTRICT CINEMA PARSER - DAILY mode
# =====================================================
def parse_district_daily(results, date_code, cutoff_minutes, now_ist):
    """
    Output fields use: sold, gross (NOT ticketsSold/grossRevenue).
    Applies a time cutoff and adds minsLeft.
    """
    detailed = []

    for res in results:
        venue_meta = res.get("venue", {}) or {}
        data = res.get("data", {}) or {}

        city = venue_meta.get("city") or venue_meta.get("City") or "Unknown"
        state = format_state(
            venue_meta.get("state") or venue_meta.get("State"), city
        )

        cinema = data.get("meta", {}).get("cinema", {}) or {}
        venue_name = cinema.get("name") or venue_meta.get("name") or venue_meta.get("district_name") or "Unknown"
        venue_addr = cinema.get("address") or venue_meta.get("address") or ""
        chain = format_chain(
            cinema.get("chainKey") or venue_meta.get("chainKey")
            or venue_meta.get("chain") or venue_name
        )

        movies = data.get("meta", {}).get("movies", []) or []
        movie_map = {str(m.get("id")): m for m in movies}

        for s in data.get("pageData", {}).get("sessions", []) or []:
            movie = movie_map.get(str(s.get("mid")))
            if not movie:
                continue

            show_time = _fmt_time(s.get("showTime"))
            if not show_time:
                continue

            mins = _minutes_left_async(show_time, now_ist)
            if mins > cutoff_minutes:
                continue

            name = movie.get("name", "Unknown")
            lang = s.get("lang") or movie.get("lang") or ""
            movie_key = _movie_label(name, s.get("scrnFmt"), lang)

            total, avail, sold, gross = _district_session_stats(s)

            detailed.append({
                "movie": movie_key,
                "city": city,
                "state": state,
                "venue": venue_name,
                "address": venue_addr,
                "time": show_time,
                "audi": s.get("audi", "") or "",
                "session_id": str(s.get("sid") or s.get("mid") or ""),
                "totalSeats": total,
                "available": avail,
                "sold": sold,
                "gross": round(gross, 2),
                "minsLeft": round(mins, 1),
                "source": "District",
                "date": date_code,
                "chain": chain,
                "movieInfo": _movie_info(movie),
            })

    return detailed


def _minutes_left_async(show_time_str, now_ist):
    """
    Calculate minutes left from now (IST). Handles post-midnight rollover.
    """
    try:
        t = datetime.strptime(show_time_str, "%I:%M %p").replace(
            year=now_ist.year,
            month=now_ist.month,
            day=now_ist.day,
            tzinfo=IST_TZ,
        )
        if t < now_ist - timedelta(hours=6):
            t += timedelta(days=1)
        return (t - now_ist).total_seconds() / 60
    except Exception:
        return 9999