"""Bounded anonymous web metadata fallback; no account/session credentials.

Request shape cross-checked against instagrapi's public media-info implementation
(September 2026). This is an undocumented Instagram endpoint, not a stable API.
"""
import json
import re
from urllib.parse import urljoin, urlsplit

from curl_cffi.requests import AsyncSession

DOC_ID = "27830990013244856"


class PublicMetadataError(ValueError):
    """Safe fixed diagnostic; never contains a response body or session value."""


async def public_homepage(session):
    url = "https://www.instagram.com/"
    for attempt in range(3):
        response = await session.get(url)
        if response.status_code not in {301, 302, 303, 307, 308}:
            if response.status_code != 200:
                raise PublicMetadataError(f"homepage_http_{response.status_code}")
            return response
        target = urljoin(url, response.headers.get("Location", ""))
        parsed = urlsplit(target)
        if (not response.headers.get("Location") or parsed.scheme != "https"
                or parsed.hostname != "www.instagram.com" or parsed.username
                or parsed.port not in {None, 443}):
            raise PublicMetadataError("homepage_redirect_disallowed")
        if attempt == 2:
            raise PublicMetadataError("homepage_redirect_limit")
        url = target



def video_info(payload: dict, shortcode: str) -> dict:
    items = ((payload.get("data") or {}).get("xdt_api__v1__media__shortcode__web_info") or {}).get("items") or []
    if not items:
        raise PublicMetadataError("metadata_empty")
    item = items[0]
    if item.get("code") != shortcode or (item.get("user") or {}).get("is_private"):
        raise PublicMetadataError("metadata_identity_or_privacy")
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
        raise PublicMetadataError("metadata_no_video")
    return {"id": shortcode, "title": ((item.get("caption") or {}).get("text") or "Instagram video")[:120],
            "duration": item.get("video_duration"), "formats": formats,
            "webpage_url": f"https://www.instagram.com/reel/{shortcode}/"}


async def extract_public_video(url: str) -> dict:
    match = re.fullmatch(r"/(?:reel|reels|p|tv)/([A-Za-z0-9_-]{1,28})/?", urlsplit(url).path)
    if not match:
        raise PublicMetadataError("unsupported_post")
    shortcode = match[1]
    async with AsyncSession(impersonate="chrome", timeout=20, allow_redirects=False) as session:
        home = await public_homepage(session)
        token = re.search(r'\["LSD",\[\],\{"token":"([^"]+)"', home.text)
        if not token:
            raise PublicMetadataError("homepage_token_missing")
        response = await session.post("https://www.instagram.com/api/graphql", headers={
            "X-FB-LSD": token[1], "X-IG-App-ID": "936619743392459",
            "X-FB-Friendly-Name": "PolarisPostRootQuery", "Origin": "https://www.instagram.com",
            "Referer": f"https://www.instagram.com/p/{shortcode}/",
        }, data={"lsd": token[1], "doc_id": DOC_ID, "server_timestamps": "true",
                 "variables": json.dumps({"shortcode": shortcode,
                    "__relay_internal__pv__PolarisShortDramaEnabledrelayprovider": False,
                    "__relay_internal__pv__PolarisMultiCaptionCarouselEnabledrelayprovider": True})})
        if response.status_code != 200:
            raise PublicMetadataError(f"graphql_http_{response.status_code}")
        try:
            payload = response.json()
        except ValueError:
            raise PublicMetadataError("graphql_not_json") from None
        if not isinstance(payload, dict):
            raise PublicMetadataError("graphql_invalid_payload")
        return video_info(payload, shortcode)
