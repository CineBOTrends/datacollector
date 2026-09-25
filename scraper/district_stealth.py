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

# District now runs as 6 parallel shards (9-14, was a single shard 9) —
# see scraper.scrape.DISTRICT_SHARD_IDS. Running them concurrently from the
# same DISTRICT_PROXIES pool with independent round-robins risks two shards
# landing on the same proxy at the same time, multiplying the request rate
# any single proxy IP sees. Instead each shard gets a disjoint slice of the
# pool (shard 9 -> proxies[0::6], shard 10 -> proxies[1::6], ...), so no two
# shards ever share a proxy, as long as there are at least 6 proxies configured.
DISTRICT_SHARD_BASE = 9
NUM_DISTRICT_SHARDS = 6

_thread_local = threading.local()
_warned_thin_pool = False


class DistrictIdentity:
    def __init__(self, shard_id=None):
        configured = os.environ.get("DISTRICT_PROXIES", "")
        all_proxies = [value.strip() for value in configured.split(",") if value.strip()]

        self.proxies = all_proxies
        if shard_id is not None and all_proxies:
            idx = (shard_id - DISTRICT_SHARD_BASE) % NUM_DISTRICT_SHARDS
            partition = all_proxies[idx::NUM_DISTRICT_SHARDS]
            if partition:
                self.proxies = partition
            else:
                # Fewer proxies configured than District shards -> can't give
                # every shard its own slice. Fall back to the shared pool
                # (old behaviour: shards may collide on the same proxy).
                global _warned_thin_pool
                if not _warned_thin_pool:
                    _warned_thin_pool = True
                    print(
                        f"! DISTRICT_PROXIES has only {len(all_proxies)} prox"
                        f"(y/ies) for {NUM_DISTRICT_SHARDS} District shards - "
                        f"shards will share proxies instead of each getting "
                        f"its own"
                    )

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


def get_district_identity(logger=None, shard_id=None):
    if not hasattr(_thread_local, "identity"):
        _thread_local.identity = DistrictIdentity(shard_id=shard_id)
        if logger:
            shard_note = f", shard {shard_id}" if shard_id is not None else ""
            logger.debug(
                f"New District identity created "
                f"({len(_thread_local.identity.proxies)} real proxies configured"
                f"{shard_note})"
            )
    return _thread_local.identity


def reset_district_identity(logger=None):
    if hasattr(_thread_local, "identity"):
        del _thread_local.identity
    if logger:
        logger.debug("District identity reset")