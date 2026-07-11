"""
Post-execution hooks.
Currently: Cloudflare cache purge after R2 upload.
"""
import os
from posixpath import dirname
from urllib.parse import urlparse

import requests

CF_ZONE_ID = os.environ.get("CF_ZONE_ID")
CF_API_TOKEN = os.environ.get("CF_API_TOKEN")
CF_PURGE_BASE_URL = os.environ.get("CF_PURGE_BASE_URL")  # e.g. https://data.cinebotrends.com


def is_cf_configured():
    return all([CF_ZONE_ID, CF_API_TOKEN, CF_PURGE_BASE_URL])


def purge_cloudflare_cache(file_url):
    """
    Purge Cloudflare cache using directory prefix.

    For R2 custom domains, prefix-based purge with directory path works.
    Format: host/dir/ (NO scheme, trailing slash)
    Example: data.cinebotrends.com/tt_gross/v1/daily/2026/03/
    """
    if not is_cf_configured():
        print("Cloudflare purge not configured, skipping")
        return False

    # Build prefix: host + dirname(path) + /
    parsed = urlparse(file_url)
    host = parsed.hostname or ""
    path = parsed.path or "/"
    dir_path = dirname(path)
    if dir_path in (".", "/"):
        prefix = f"{host}/"
    else:
        prefix = f"{host}{dir_path}/"

    try:
        resp = requests.post(
            f"https://api.cloudflare.com/client/v4/zones/{CF_ZONE_ID}/purge_cache",
            headers={
                "Authorization": f"Bearer {CF_API_TOKEN}",
                "Content-Type": "application/json",
            },
            json={"prefixes": [prefix]},
            timeout=30,
        )

        data = resp.json()
        if data.get("success"):
            print(f"Cloudflare cache purged (prefix: {prefix})")
            return True
        else:
            errors = data.get("errors", [])
            print(f"Cloudflare purge failed: {errors}")
            return False

    except Exception as e:
        print(f"Cloudflare purge error: {e}")
        return False


def purge_after_r2_upload(r2_key):
    """
    Build the full CDN URL from R2 key and purge its directory prefix.

    Args:
        r2_key: The R2 object key (before prefix), e.g. "v1/daily/2026/03/17.json"
    """
    if not is_cf_configured():
        return

    from services.r2_storage import R2_KEY_PREFIX

    base_url = CF_PURGE_BASE_URL.rstrip("/")

    if R2_KEY_PREFIX:
        full_path = f"{R2_KEY_PREFIX.rstrip('/')}/{r2_key.lstrip('/')}"
    else:
        full_path = r2_key

    file_url = f"{base_url}/{full_path}"
    purge_cloudflare_cache(file_url)
