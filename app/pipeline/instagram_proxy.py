"""Optional operator-configured route, scoped to Instagram requests only."""

import os
from urllib.parse import urlsplit


class InstagramProxyConfigError(ValueError):
    def __init__(self):
        super().__init__("instagram_proxy_config_invalid")


def instagram_proxy_url() -> str | None:
    value = os.getenv("INSTAGRAM_PROXY_URL", "").strip()
    if not value:
        return None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https", "socks5", "socks5h"}
                or not parsed.hostname or parsed.port == 0
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
                or any(c.isspace() for c in value)):
            raise ValueError
    except ValueError:
        raise InstagramProxyConfigError() from None
    return value
