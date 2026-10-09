import re, json, requests

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/154.0.0.0 Safari/537.36",
    "Accept-Language": "en-IN,en;q=0.9",
}
IMG = "https://assets-in.bmscdn.com/iedb/movies/images/mobile"

def find_key(obj, key):
    """first value of `key` anywhere in nested JSON"""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            r = find_key(v, key)
            if r not in (None, ""):
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_key(v, key)
            if r not in (None, ""):
                return r
    return None

def raw(html, key):
    """fallback: pull "key":"value" or "key":[...] straight from the HTML"""
    m = re.search(rf'"{key}":("(?:[^"\\]|\\.)*"|\[[^\]]*\])', html)
    return json.loads(m.group(1)) if m else None

def iso_minutes(s):
    m = re.match(r'PT(?:(\d+)H)?(?:(\d+)M)?', s or "")
    return int(m.group(1) or 0) * 60 + int(m.group(2) or 0) if m else 0

def get_movie(page):
    html = requests.get(page, headers=HEADERS, timeout=15).text

    start = html.index("{", html.index("window.__INITIAL_STATE__"))
    state, _ = json.JSONDecoder().raw_decode(html[start:])

    # JSON-LD Movie
    ld = {}
    for b in re.findall(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', html, re.S):
        d = json.loads(b)
        if isinstance(d, dict) and d.get("@type") == "Movie":
            ld = d

    # the dynamic synopsis query (key changes per movie/city)
    queries = state.get("synopsisMoviesApi", {}).get("queries", {})
    dyn = next((v.get("data", {}) for k, v in queries.items()
                if k.startswith("fetchSynopsisInitDynamic")), {})
    banner = dyn.get("bannerWidget", {})

    code = (find_key(dyn, "eventDefaultCode") or raw(html, "eventDefaultCode") or "")
    image_code = (find_key(dyn, "eventImageCode") or raw(html, "eventImageCode") or "")

    poster = banner.get("bannerImageUrl") or (f"{IMG}/listing/xxlarge/{image_code}.jpg" if image_code else "")
    thumb = (banner.get("multimedia", {}).get("objectData", {}).get("imageUrl")
             or (f"{IMG}/thumbnail/xlarge/{image_code}.jpg" if image_code else ""))
    cover = next((u for u in re.findall(
        rf'https://assets-in\.bmscdn\.com/discovery-catalog/events/{code.lower()}-[^"\'\s\\]*landscape\.jpg', html)), "")

    genre = find_key(state, "eventGenre") or raw(html, "eventGenre") or ""
    lang = find_key(state, "eventLanguage") or raw(html, "eventLanguage") or ld.get("inLanguage", [])
    rating = raw(html, "rating_percentage") or ""

    return {
        "movie": find_key(state, "eventName") or raw(html, "eventName") or ld.get("name"),
        "source": "BookMyShow",
        "movieInfo": {
            "contentId": code,
            "name": find_key(state, "eventName") or raw(html, "eventName") or ld.get("name"),
            "lang": lang,
            "scrnFmt": "",
            "sndFmt": "",
            "censor": find_key(state, "eventCensor") or raw(html, "eventCensor") or "",
            "duration": iso_minutes(ld.get("duration")),
            "genres": genre.split("|") if isinstance(genre, str) else genre,
            "poster": poster,
            "cover": cover,
            "thumbnail": thumb,
            "trailer": "",
            "rating": {"value": rating, "imageUrl": ""},
            "releaseDate": find_key(state, "releaseDate") or raw(html, "releaseDate") or ld.get("datePublished"),
            "imageCode": image_code,
        },
    }

if __name__ == "__main__":
    pages = [
        "https://in.bookmyshow.com/movies/bengaluru/heart-of-the-beast/ET00504928",
        "https://in.bookmyshow.com/movies/bengaluru/the-paradise/ET00436621",
    ]
    out = [get_movie(p) for p in pages]
    print(json.dumps(out, indent=2, ensure_ascii=False))
    json.dump(out, open("bms_movies.json", "w", encoding="utf-8"), indent=2, ensure_ascii=False)