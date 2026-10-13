"""Shared, credential-safe Webshare proxy configuration."""
import os
import re
from urllib.parse import urlsplit


_ENV_NAME = "WEBSHARE_PROXY"


def _local_proxy_value():
    """Read the proxy URI from the project .env without loading other secrets."""
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    try:
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                match = re.match(r"^\s*(?:\$env:)?WEBSHARE_PROXY\s*=\s*(.*?)\s*$", line)
                if not match:
                    continue
                value = match.group(1)
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                    value = value[1:-1]
                if value:
                    return value
    except FileNotFoundError:
        return ""
    return ""


def get_webshare_proxy():
    """Return a validated HTTP(S) proxy URL, or None when not configured.

    The process environment takes precedence over the repository-local .env.
    The URL is never printed or included in validation errors.
    """
    value = os.environ.get(_ENV_NAME, "").strip() or _local_proxy_value().strip()
    if not value:
        return None

    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme in ("http", "https")
            and parsed.hostname
            and parsed.port
            and parsed.username
            and parsed.password
        )
    except ValueError:
        valid = False

    if not valid:
        raise ValueError(
            "WEBSHARE_PROXY must be an HTTP(S) URL with username, password, host, and port"
        )
    return value


def proxy_dict(proxy_url=None):
    """Return the requests-style proxy mapping for a configured gateway."""
    proxy_url = proxy_url or get_webshare_proxy()
    if not proxy_url:
        return None
    return {"http": proxy_url, "https": proxy_url}


def redact_proxy_credentials(message):
    """Mask any URL user-info before exception text is written to logs."""
    return re.sub(
        r"(?i)(https?://)[^/@\s]+@",
        r"\1<redacted>@",
        str(message),
    )
