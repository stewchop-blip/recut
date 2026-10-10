"""Last-resort public metadata lookup on independent egress.

Uses SnapInsta.nl's public website request, not a guaranteed integration API.
Only a canonical public post URL leaves ReCut; media transfers directly from
Instagram CDN through the existing downloader. No cookies or account required.
"""
import asyncio
import json
import os
import re
from urllib.parse import urlsplit

from curl_cffi.requests import AsyncSession

from app.core.logging import get_logger
from app.pipeline.instagram_public import allowed_media_url

logger = get_logger(__name__)
ENDPOINT = "https://snapinsta.nl/fetch-instagram-media"
TIMEOUT = 30
MAX_RESPONSE_BYTES = 256 * 1024


def public_shortcode(url):
    try:
        parsed = urlsplit(url)
        match = re.fullmatch(r"/(?:p|reel|reels|tv)/([A-Za-z0-9_-]{1,28})/?", parsed.path)
        if (match and parsed.scheme == "https" and parsed.hostname in
                {"instagram.com", "www.instagram.com", "instagr.am"}
                and not parsed.username and not parsed.password
                and parsed.port in {None, 443}):
            return match[1]
    except (ValueError, TypeError):
        pass
    return None


def external_metadata(payload, code):
    if not isinstance(payload, dict) or payload.get("success") is not True:
        return None
    data = payload.get("data")
    if (not isinstance(data, dict) or public_shortcode(data.get("canonical_url")) != code
            or data.get("content_kind") not in {"reel", "post", "video"}):
        return None
    items = data.get("items")
    # Don't silently substitute a carousel item for the requested video.
    if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
        return None
    item = items[0]
    if item.get("id") != code or item.get("media_type") != "video":
        return None
    duration = item.get("duration_seconds")
    if duration is not None and (type(duration) not in {int, float} or not 0 < duration < 1_000_000):
        return None
    # High-resolution variants can be video-only. Preserve original audio.
    formats = []
    for variant in item.get("variants") or []:
        if (not isinstance(variant, dict) or variant.get("hasAudio") is not True
                or variant.get("ext") != "mp4" or not allowed_media_url(variant.get("url"))):
            continue
        dimensions = [variant.get("width"), variant.get("height")]
        if any(n is not None and (type(n) is not int or not 0 < n <= 4096) for n in dimensions):
            continue
        formats.append({"url": variant["url"], "ext": "mp4",
                        "width": dimensions[0], "height": dimensions[1]})
    if not formats:
        return None
    return {"id": code, "title": "Instagram video", "duration": duration,
            "webpage_url": f"https://www.instagram.com/reel/{code}/", "formats": formats}


async def extract_external_video(url):
    """Bounded, nonfatal fallback; disable with INSTAGRAM_EXTERNAL_FALLBACK=false."""
    if os.getenv("INSTAGRAM_EXTERNAL_FALLBACK", "true").lower() in {"0", "false", "off"}:
        return None
    code = public_shortcode(url)
    if not code:
        return None
    logger.info("instagram_external_start", provider="snapinsta_nl")
    try:
        async with asyncio.timeout(TIMEOUT):
            async with AsyncSession(impersonate="chrome", timeout=TIMEOUT,
                                    allow_redirects=False) as session:
                async with session.stream("POST", ENDPOINT,
                    json={"instagram_url": f"https://www.instagram.com/reel/{code}/"},
                    headers={"Origin": "https://snapinsta.nl", "Referer": "https://snapinsta.nl/"}) as response:
                    if response.status_code != 200:
                        logger.warning("instagram_external_failed", reason="http_error",
                                       status=response.status_code)
                        return None
                    body = bytearray()
                    async for chunk in response.aiter_content():
                        if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                            raise ValueError
                        body.extend(chunk)
                metadata = external_metadata(json.loads(body), code)
    except TimeoutError:
        logger.warning("instagram_external_failed", reason="timeout")
        return None
    except Exception:
        # Never log provider messages, captions, signed URLs or transport errors.
        logger.warning("instagram_external_failed", reason="transport_or_invalid_response")
        return None
    logger.info("instagram_external_ok" if metadata else "instagram_external_no_video")
    return metadata
