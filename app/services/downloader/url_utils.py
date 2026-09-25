"""URL utils — adapted from reels-downloader-bot url_utils.py (Apache-2.0).

urlparse-based validation with hostname BOUNDARY checks (not substring),
normalization, tracking-param stripping.
"""
from __future__ import annotations

from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse
import re

# Hostname allowlist (TZ constraint) with exact-host boundary
SUPPORTED_HOSTS = {
    "youtube.com": "youtube",
    "youtu.be": "youtube",
    "tiktok.com": "tiktok",
    "instagram.com": "instagram",
    "instagr.am": "instagram",
    "vm.tiktok.com": "tiktok",
}

# Tracking params safe to strip (audit #6)
STRIP_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_content",
                "utm_term", "si", "feature", "ref", "igshid", "is_from_webapp"}


def _host_matches(url_host: str, supported: str) -> bool:
    """Hostname boundary: tiktok.com matches www.tiktok.com, not faketiktok.com."""
    host = url.lower().strip(".")
    return host == supported or host.endswith("." + supported)


def get_platform_name(url: str) -> str:
    """Platform id for a supported URL, else ''. Hostname-boundary safe."""
    try:
        host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    except ValueError:
        return ""
    for supported, platform in SUPPORTED_HOSTS.items():
        if host == supported or host.endswith("." + supported):
            return platform
    return ""


def is_supported_url(url: str) -> bool:
    return bool(get_platform_name(url))


def normalize_url(url: str) -> str:
    """Strip tracking params; keep everything else intact."""
    try:
        p = urlparse(url)
    except ValueError:
        return url
    qs = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
          if k.lower() not in STRIP_PARAMS]
    return p._replace(query=encodeURIComponent(qs)).geturl()


def encodeURIComponent(qs) -> str:  # noqa: N802 (keeps call sites readable)
    from urllib.parse import quote
    return urlencode(qs, quote_via=quote)


URL_RE = None  # extracted below


def extract_url(text: str) -> str | None:
    """First supported URL inside a text message (or None)."""
    import re
    if not text:
        return None
    m = re.search(r"https?://\S+", text)
    if not m:
        return None
    url = m.group(0).rstrip(".,;:!?)\"'")
    return url if is_supported_url(url) else None