"""Bounded anonymous web metadata fallback; no account/session credentials.

Request shape cross-checked against instagrapi's public media-info implementation
(September 2026). This is an undocumented Instagram endpoint, not a stable API.
"""
import json
import re
from urllib.parse import urlsplit

from curl_cffi.requests import AsyncSession

DOC_ID = "27830990013244856"


def video_info(payload: dict, shortcode: str) -> dict:
    items = (payload.get("data", {}).get("xdt_api__v1__media__shortcode__web_info") or {}).get("items") or []
    if not items:
        raise ValueError("No public media")
    item = items[0]
    if item.get("code") != shortcode or (item.get("user") or {}).get("is_private"):
        raise ValueError("Public media does not match")
    formats = []
    for version in item.get("video_versions") or []:
        url = version.get("url") or ""
        host = urlsplit(url).hostname or ""
        if (urlsplit(url).scheme != "https" or urlsplit(url).username
                or not host.endswith((".cdninstagram.com", ".fbcdn.net"))):
            continue
        formats.append({"url": url, "ext": "mp4", "width": version.get("width"),
                        "height": version.get("height")})
    if not formats:
        raise ValueError("No public video formats")
    return {"id": shortcode, "title": ((item.get("caption") or {}).get("text") or "Instagram video")[:120],
            "duration": item.get("video_duration"), "formats": formats,
            "webpage_url": f"https://www.instagram.com/reel/{shortcode}/"}


async def extract_public_video(url: str) -> dict:
    match = re.fullmatch(r"/(?:reel|reels|p|tv)/([A-Za-z0-9_-]{1,28})/?", urlsplit(url).path)
    if not match:
        raise ValueError("Unsupported public post")
    shortcode = match[1]
    async with AsyncSession(impersonate="chrome", timeout=20, allow_redirects=False) as session:
        home = await session.get("https://www.instagram.com/")
        if home.status_code != 200:
            raise ValueError("Public session unavailable")
        token = re.search(r'\["LSD",\[\],\{"token":"([^"]+)"', home.text)
        if not token:
            raise ValueError("Public page token missing")
        response = await session.post("https://www.instagram.com/api/graphql", headers={
            "X-FB-LSD": token[1], "X-IG-App-ID": "936619743392459",
            "X-FB-Friendly-Name": "PolarisPostRootQuery", "Origin": "https://www.instagram.com",
            "Referer": f"https://www.instagram.com/p/{shortcode}/",
        }, data={"lsd": token[1], "doc_id": DOC_ID, "server_timestamps": "true",
                 "variables": json.dumps({"shortcode": shortcode,
                    "__relay_internal__pv__PolarisShortDramaEnabledrelayprovider": False,
                    "__relay_internal__pv__PolarisMultiCaptionCarouselEnabledrelayprovider": True})})
        if response.status_code != 200:
            raise ValueError("Public metadata unavailable")
        return video_info(response.json(), shortcode)
