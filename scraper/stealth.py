"""
Stealth / Anti-Bot Detection Bypass Module.

ALL techniques preserved exactly from the original codebase:
- User Agent rotation (3 Chrome UAs, random per identity)
- Fake IP spoofing via X-Forwarded-For header
- CloudScraper for Cloudflare JS challenge bypass
- Thread-local identity management with reset on errors
- Realistic browser headers (Accept, Referer, Origin)

Supports multiple sites, each with its own Origin/Referer, while sharing
the same UA rotation, IP spoofing, and CloudScraper bypass:
- bookmyshow: https://in.bookmyshow.com
- district:   https://www.district.in
"""
import random
import threading

import cloudscraper

# =====================================================
# USER AGENTS
# =====================================================
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/119 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/118 Safari/537.36",
]

# Per-site Origin/Referer used for realistic browser headers.
SITE_CONFIG = {
    "bookmyshow": {
        "origin": "https://in.bookmyshow.com",
        "referer": "https://in.bookmyshow.com/",
    },
    "district": {
        "origin": "https://www.district.in",
        "referer": "https://www.district.in/",
    },
}

thread_local = threading.local()


class Identity:
    def __init__(self, site="bookmyshow"):
        self.site = site
        config = SITE_CONFIG.get(site, SITE_CONFIG["bookmyshow"])
        self.origin = config["origin"]
        self.referer = config["referer"]

        self.ua = random.choice(USER_AGENTS)
        self.ip = ".".join(str(random.randint(20, 230)) for _ in range(4))
        self.scraper = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "desktop": True}
        )

    def headers(self):
        return {
            "User-Agent": self.ua,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-IN,en;q=0.9",
            "Origin": self.origin,
            "Referer": self.referer,
            "X-Forwarded-For": self.ip,
        }


def get_identity(site="bookmyshow", logger=None):
    """
    Get (or lazily create) the thread-local identity for a given site.
    Each thread keeps one identity per site so CloudScraper sessions/cookies
    are reused across requests to that site.
    """
    if not hasattr(thread_local, "identities"):
        thread_local.identities = {}

    if site not in thread_local.identities:
        thread_local.identities[site] = Identity(site)
        if logger:
            logger.debug(f"New identity created for {site}")

    return thread_local.identities[site]


def reset_identity(logger=None, site=None):
    """
    Reset identity/identities for the current thread.
    If `site` is given, only that site's identity is dropped; otherwise all
    cached identities for this thread are cleared.
    """
    identities = getattr(thread_local, "identities", None)
    if identities:
        if site:
            identities.pop(site, None)
        else:
            identities.clear()
    if logger:
        logger.debug(f"Identity reset{f' for {site}' if site else ''}")
