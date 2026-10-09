"""
Resolve a BookMyShow event code (ET00504928) to its poster image id
(heart-of-the-beast-et00504928-1782473655) by reading the public movie page,
then build the xxlarge background URL.

The showtimes APIs only expose the event code; the timestamped image id is
only on the movie page. Results are cached in bms_images.json.
"""
import json
import os
import re

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_FILE = os.path.join(HERE, "bms_images.json")
TITLES_FILE = os.path.join(HERE, "bms_titles.json")
PAGE_URL = "https://in.bookmyshow.com/hyderabad/movies/x/{code}"
BG_URL = "https://assets-in.bmscdn.com/iedb/movies/images/mobile/listing/xxlarge/{id}.jpg"

_cache = None


def _load():
    global _cache
    if _cache is None:
        try:
            with open(CACHE_FILE, encoding="utf-8-sig") as f:
                _cache = json.load(f)
        except (OSError, ValueError):
            _cache = {}
    return _cache


def _save():
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(_cache, f, indent=2, sort_keys=True)
    except OSError:
        pass


BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "en-IN,en;q=0.9",
}


def _fetchers(logger=None):
    """Page fetchers to try in order: scraper session, fresh session, plain requests.
    Each returns the page HTML, or None when blocked (Cloudflare challenge)."""
    def via_identity(reset=False):
        def run(code):
            from scraper import stealth
            if reset:
                stealth.reset_identity(logger)
            ident = stealth.get_identity(logger)
            r = ident.scraper.get(PAGE_URL.format(code=code), headers=ident.headers(), timeout=25)
            return r.text if r.status_code == 200 else None
        return run

    def plain(code):
        import requests
        r = requests.get(PAGE_URL.format(code=code), headers=BROWSER_HEADERS, timeout=25)
        return r.text if r.status_code == 200 else None

    return [via_identity(), via_identity(reset=True), plain]


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


_listing_cache = None
LISTING_CITIES = ["mumbai", "hyderabad", "bengaluru", "chennai", "delhi-ncr", "kolkata",
                  "kochi", "pune", "ahmedabad"]
LISTING_PAGES = ["https://in.bookmyshow.com/explore/upcoming-movies-{c}",
                 "https://in.bookmyshow.com/explore/movies-{c}"]
_CARD_RE = re.compile(
    r'"ctaUrl":"https://in\.bookmyshow\.com/movies/[a-z0-9-]+/([a-z0-9-]+)/(ET\d{8})"'
    r'.{0,400}?"type":"text","text":"([^"]+)"', re.S)


def _scan_listings(wanted, logger=None):
    """Scan BMS now-showing + upcoming pages; return {norm(title or url slug): code}.
    Stops early once every wanted title is found."""
    import requests
    found = {}
    for city in LISTING_CITIES:
        for page in LISTING_PAGES:
            try:
                r = requests.get(page.format(c=city), headers=BROWSER_HEADERS, timeout=25)
            except Exception:
                continue
            if r.status_code != 200:
                continue
            for slug, code, title in _CARD_RE.findall(r.text):
                found.setdefault(_norm(slug), code)
                found.setdefault(_norm(title), code)
            if all(w in found for w in wanted):
                return found
    return found


def find_code(title, logger=None):
    """Event code for a movie title (any language/spelling variant is NOT merged:
    the title must match BMS's title or URL slug). Cached in bms_titles.json."""
    key = _norm(title)
    if not key:
        return None
    try:
        with open(TITLES_FILE, encoding="utf-8-sig") as f:
            known = json.load(f)
    except (OSError, ValueError):
        known = {}
    if known.get(key):
        return known[key]
    global _listing_cache
    if _listing_cache is None or key not in _listing_cache:
        _listing_cache = {**(_listing_cache or {}), **_scan_listings([key], logger)}
    code = _listing_cache.get(key)
    if code:
        known[key] = code
        try:
            with open(TITLES_FILE, "w", encoding="utf-8") as f:
                json.dump(known, f, indent=2, sort_keys=True)
        except OSError:
            pass
    return code


def resolve_image_id(code, logger=None):
    """Return the BMS image id for an event code, or None."""
    code = (code or "").strip().upper()
    if not re.fullmatch(r"ET\d{8}", code):
        return None
    cache = _load()
    if cache.get(code):
        return cache[code]
    pat = r"[a-z0-9][a-z0-9-]*-%s-\d{9,11}" % code.lower()
    m = None
    for fetch in _fetchers(logger):
        try:
            html = fetch(code)
        except Exception as e:
            print(f"    ! BMS image lookup attempt failed for {code} ({e})")
            continue
        if html:
            m = re.search(r'"eventImageCode"\s*:\s*"(%s)"' % pat, html) or re.search(pat, html)
            if m:
                break
    if not m:
        return None
    cache[code] = m.group(m.lastindex or 0)
    _save()
    return cache[code]
