"""
Synchronous fetcher using cloudscraper (shards 1-8).

Stealth measures preserved:
- CloudScraper for Cloudflare bypass
- Identity-based headers with fake IP
- Hard timeout via threading (cross-platform)
- Status code checking (429, 403, HTML block detection)
"""
import threading

from scraper.stealth import get_identity

# =====================================================
# CONFIG
# =====================================================
API_TIMEOUT = 12
HARD_TIMEOUT_SECONDS = 15

BMS_API_URL = (
    "https://in.bookmyshow.com/api/v2/mobile/showtimes/byvenue"
    "?venueCode={venue_code}&dateCode={date_code}"
)


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
# FETCH VENUE
# =====================================================
@hard_timeout(HARD_TIMEOUT_SECONDS)
def _do_fetch(ident, venue_code, date_code):
    """
    Actual HTTP call — runs inside timeout wrapper (may be daemon thread).
    Identity is passed explicitly so it works across thread boundaries.
    """
    url = BMS_API_URL.format(venue_code=venue_code, date_code=date_code)
    r = ident.scraper.get(url, headers=ident.headers(), timeout=API_TIMEOUT)

    # Check HTTP status code
    if hasattr(r, "status_code") and r.status_code != 200:
        if r.status_code == 429:
            raise RuntimeError(f"RateLimit|{r.status_code}")
        elif r.status_code == 403:
            raise RuntimeError(f"Blocked|{r.status_code}")
        else:
            raise RuntimeError(f"HTTPError|{r.status_code}")

    # Check if response is HTML (blocked)
    if not r.text.strip().startswith("{"):
        raise RuntimeError("Blocked|HTML")

    return r.json()


def fetch_venue(venue_code, date_code, logger=None):
    """
    Public API. Gets identity in the CALLING thread (where thread_local
    persists across requests), then passes it to the timed fetch.

    This ensures CloudScraper session + cookies are reused, preventing
    bot detection from seeing rapid session churn.
    """
    ident = get_identity(logger)
    return _do_fetch(ident, venue_code, date_code)
