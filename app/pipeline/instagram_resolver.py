"""Optional bounded metadata resolver; never transfers media or account cookies."""
import asyncio
import json
import os
import re
from urllib.parse import urlsplit

import httpx

from app.core.logging import get_logger
from app.pipeline.instagram_public import allowed_media_url

logger = get_logger(__name__)
RESOLVER_TIMEOUT = 28
MAX_RESPONSE_BYTES = 32768
SOURCES = {"embed", "post_html", "graphql_post_root_271"}


def resolver_config():
    endpoint = os.getenv("INSTAGRAM_RESOLVER_URL", "").strip()
    secret = os.getenv("INSTAGRAM_RESOLVER_SECRET", "")
    if not endpoint:
        return None
    try:
        parsed = urlsplit(endpoint)
        if (parsed.scheme != "https" or not (parsed.hostname or "").endswith(".workers.dev")
                or parsed.username or parsed.password or parsed.port not in {None, 443}
                or parsed.path != "/instagram" or parsed.query or parsed.fragment
                or not 32 <= len(secret) <= 256 or any(c.isspace() for c in endpoint)
                or "\n" in secret or "\r" in secret):
            raise ValueError
    except ValueError:
        logger.warning("instagram_resolver_invalid_response", reason="config_invalid")
        return None
    return endpoint, secret


def resolver_metadata(payload, code):
    if not isinstance(payload, dict):
        raise ValueError
    if payload.get("ok") is not True:
        logger.warning("instagram_resolver_no_video")
        return None
    if (payload.get("shortcode") != code or payload.get("source") not in SOURCES
            or not allowed_media_url(payload.get("video_url"))):
        raise ValueError
    duration = payload.get("duration")
    if duration is not None and (type(duration) not in {int, float}
                                or not 0 < duration < 1_000_000):
        raise ValueError
    dimensions = [payload.get("width"), payload.get("height")]
    if any(n is not None and (type(n) is not int or not 0 < n <= 4096) for n in dimensions):
        raise ValueError
    return {"id": code, "title": "Instagram video", "duration": duration,
            "webpage_url": f"https://www.instagram.com/reel/{code}/",
            "formats": [{"url": payload["video_url"], "ext": "mp4",
                         "width": dimensions[0], "height": dimensions[1]}]}


async def extract_resolved_video(url):
    """Return compatible metadata or None; disabled/unavailable is non-fatal."""
    config = resolver_config()
    if not config:
        return None
    try:
        parsed = urlsplit(url)
        match = re.fullmatch(r"/(?:p|reel)/([A-Za-z0-9_-]{1,28})/?", parsed.path)
        if (not match or parsed.scheme != "https" or parsed.hostname not in
                {"instagram.com", "www.instagram.com"} or parsed.username or parsed.password
                or parsed.port not in {None, 443}):
            raise ValueError
    except ValueError:
        logger.warning("instagram_resolver_invalid_response", reason="input_invalid")
        return None
    endpoint, secret = config
    code = match[1]
    logger.info("instagram_resolver_request_started")
    try:
        async with asyncio.timeout(RESOLVER_TIMEOUT):
            async with httpx.AsyncClient(timeout=RESOLVER_TIMEOUT, follow_redirects=False,
                                         trust_env=False) as client:
                async with client.stream("GET", endpoint,
                    params={"url": f"https://www.instagram.com/reel/{code}/"},
                    headers={"X-ReCut-Resolver-Secret": secret}) as response:
                    if response.status_code != 200:
                        logger.warning(f"instagram_resolver_http_{response.status_code}")
                        return None
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > MAX_RESPONSE_BYTES:
                            raise ValueError
                metadata = resolver_metadata(json.loads(body), code)
    except (TimeoutError, httpx.TimeoutException):
        logger.warning("instagram_resolver_timeout")
        return None
    except Exception:
        # HTTP/JSON exceptions may include request URLs; never log their text.
        logger.warning("instagram_resolver_invalid_response")
        return None
    if metadata:
        logger.info("instagram_resolver_success")
    return metadata
