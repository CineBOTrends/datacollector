"""
Stealth / Anti-Bot Detection Bypass Module.

ALL techniques preserved exactly from the original codebase:
- User Agent rotation (3 Chrome UAs, random per identity)
- Fake IP spoofing via X-Forwarded-For header
- CloudScraper for Cloudflare JS challenge bypass
- Thread-local identity management with reset on errors
- Realistic browser headers (Accept, Referer, Origin)
"""
import os
import random
import threading

import cloudscraper

from scraper.proxy_config import proxy_dict as _webshare_proxy_dict

# =====================================================
# USER AGENTS
# =====================================================
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/119 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/118 Safari/537.36",
]

thread_local = threading.local()


class Identity:
    def __init__(self):
        self.ua = random.choice(USER_AGENTS)
        self.ip = ".".join(str(random.randint(20, 230)) for _ in range(4))
        self.scraper = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "desktop": True}
        )
        # Chrome-TLS-fingerprint session: BMS 403s CloudScraper's fingerprint.
        try:
            from curl_cffi import requests as _cffi
            self.cffi = _cffi.Session(impersonate="chrome124")
        except Exception:
            self.cffi = None

    def cffi_headers(self):
        # User-Agent / client hints come from the impersonated browser profile.
        return {
            "Accept-Language": "en-IN,en;q=0.9",
            "Origin": "https://in.bookmyshow.com",
            "Referer": "https://in.bookmyshow.com/",
        }

    def proxy_dict(self):
        # Opt-in: BMS 403s rotating residential IPs, so it goes direct unless
        # BMS_USE_PROXY=1 is set.
        if os.environ.get("BMS_USE_PROXY", "").strip().lower() not in ("1", "true", "yes"):
            return None
        return _webshare_proxy_dict()

    def headers(self):
        return {
            "User-Agent": self.ua,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-IN,en;q=0.9",
            "Origin": "https://in.bookmyshow.com",
            "Referer": "https://in.bookmyshow.com/",
            "X-Forwarded-For": self.ip,
        }


def get_identity(logger=None):
    if not hasattr(thread_local, "identity"):
        thread_local.identity = Identity()
        if logger:
            logger.debug("New identity created")
    return thread_local.identity


def reset_identity(logger=None):
    if hasattr(thread_local, "identity"):
        del thread_local.identity
    if logger:
        logger.debug("Identity reset")
