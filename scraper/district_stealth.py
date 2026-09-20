"""
District-only Stealth / Anti-Bot Detection Bypass Module.

Same techniques as BookMyShow's stealth.py, applied to District:
- User Agent rotation (3 Chrome UAs, random per identity)
- Fake IP spoofing via X-Forwarded-For header
- CloudScraper for Cloudflare JS challenge bypass
- Thread-local identity management with reset on errors
- Realistic browser headers (Accept, Referer, Origin)

Kept as a SEPARATE module from stealth.py (BookMyShow's) on purpose. District
additionally supports optional real proxy rotation via the DISTRICT_PROXIES
env var (comma-separated proxy URLs) — BookMyShow does not use this.
"""
import os
import random
import threading

import cloudscraper

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/119 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/118 Safari/537.36",
]

_thread_local = threading.local()


class DistrictIdentity:
    def __init__(self):
        configured = os.environ.get("DISTRICT_PROXIES", "")
        self.proxies = [value.strip() for value in configured.split(",") if value.strip()]
        self.index = random.randrange(len(self.proxies)) if self.proxies else 0
        self.ua = random.choice(USER_AGENTS)
        self.fake_ip = ".".join(str(random.randint(20, 230)) for _ in range(4))
        self.scraper = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "desktop": True}
        )

    def headers(self):
        return {
            "User-Agent": self.ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-IN,en;q=0.9",
            "Origin": "https://www.district.in",
            "Referer": "https://www.district.in/",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin",
            "X-Forwarded-For": self.fake_ip,
        }

    def proxy_dict(self):
        """Return a requests-style proxies dict using the next configured
        real proxy (round-robin), or None if no DISTRICT_PROXIES are set."""
        if not self.proxies:
            return None
        proxy = self.proxies[self.index % len(self.proxies)]
        self.index += 1
        return {"http": proxy, "https": proxy}


def get_district_identity(logger=None):
    if not hasattr(_thread_local, "identity"):
        _thread_local.identity = DistrictIdentity()
        if logger:
            logger.debug(
                f"New District identity created "
                f"({len(_thread_local.identity.proxies)} real proxies configured)"
            )
    return _thread_local.identity


def reset_district_identity(logger=None):
    if hasattr(_thread_local, "identity"):
        del _thread_local.identity
    if logger:
        logger.debug("District identity reset")
