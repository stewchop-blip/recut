"""Bounded anonymous Instagram metadata pipeline; no account credentials.

Independent adapter for undocumented endpoints; see docs/INSTAGRAM_DOWNLOAD.md.
"""
import asyncio
import json
import re
from urllib.parse import urljoin, urlsplit

from curl_cffi.requests import AsyncSession

from app.core.logging import get_logger

logger = get_logger(__name__)
REQUEST_TIMEOUT = 10
MOBILE_HEADERS = {
    "User-Agent": "Instagram 275.0.0.27.98 Android (33/13; 280dpi; 720x1423; "
                  "Xiaomi; Redmi 7; onclite; qcom; en_US; 458229237)",
    "Accept": "application/json",
    "Accept-Language": "en-US",
    "X-IG-App-Locale": "en_US",
    "X-IG-Device-Locale": "en_US",
    "X-IG-Mapped-Locale": "en_US",
}

DOC_ID = "27830990013244856"


class PublicMetadataError(ValueError):
    """Safe fixed diagnostic; never contains a response body or session value."""


async def public_homepage(session):
    url = "https://www.instagram.com/"
    for attempt in range(3):
        response = await session.get(url, timeout=5)
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
    data = payload.get("data")
    if not isinstance(data, dict):
        errors = payload.get("errors") or []
        codes = [str(e.get("code")) for e in errors if isinstance(e, dict)
                 and type(e.get("code")) is int][:3]
        suffix = "_" + "_".join(codes) if codes else ""
        raise PublicMetadataError(("graphql_errors" if errors else "graphql_data_missing") + suffix)
    items = (data.get("xdt_api__v1__media__shortcode__web_info") or {}).get("items") or []
    if items:
        item = items[0]
    else:
        # Public web query deployments expose either v1 or shortcode media.
        media = data.get("xdt_shortcode_media") or data.get("shortcode_media")
        if not isinstance(media, dict):
            raise PublicMetadataError("metadata_items_missing" if
                "xdt_api__v1__media__shortcode__web_info" in data else "metadata_schema_unknown")
        item = {"code": media.get("shortcode"), "user": media.get("owner"),
                "video_duration": media.get("video_duration"),
                "video_versions": [{"url": media.get("video_url"),
                    "width": (media.get("dimensions") or {}).get("width"),
                    "height": (media.get("dimensions") or {}).get("height")} ]}
    if item.get("code") != shortcode or (item.get("user") or {}).get("is_private"):
        raise PublicMetadataError("metadata_identity_or_privacy")
    formats = []
    for version in item.get("video_versions") or []:
        if not isinstance(version, dict) or not allowed_media_url(version.get("url")):
            continue
        width, height = version.get("width"), version.get("height")
        if any(value is not None and (type(value) is not int or not 0 < value <= 4096)
               for value in (width, height)):
            continue
        formats.append({"url": version["url"], "ext": "mp4", "width": width,
                        "height": height})
    formats.sort(key=lambda f: (f["width"] or 0) * (f["height"] or 0))
    if not formats:
        raise PublicMetadataError("metadata_no_video")
    return {"id": shortcode, "title": ((item.get("caption") or {}).get("text") or "Instagram video")[:120],
            "duration": item.get("video_duration"), "formats": formats,
            "webpage_url": f"https://www.instagram.com/reel/{shortcode}/"}


def allowed_media_url(url) -> bool:
    if not isinstance(url, str):
        return False
    try:
        parsed = urlsplit(url)
        return (parsed.scheme == "https" and not parsed.username and not parsed.password
                and parsed.port in {None, 443}
                and (parsed.hostname or "").endswith((".cdninstagram.com", ".fbcdn.net")))
    except ValueError:
        return False


async def public_json(session, url, stage, **kwargs):
    response = await session.get(url, timeout=REQUEST_TIMEOUT, **kwargs)
    if response.status_code != 200:
        raise PublicMetadataError(f"{stage}_http_{response.status_code}")
    try:
        payload = response.json()
    except ValueError:
        raise PublicMetadataError(f"{stage}_not_json") from None
    if not isinstance(payload, dict):
        raise PublicMetadataError(f"{stage}_invalid_payload")
    return payload


async def public_oembed(session, shortcode):
    payload = await public_json(session, "https://i.instagram.com/api/v1/oembed/", "oembed",
        params={"url": f"https://www.instagram.com/p/{shortcode}/"}, headers=MOBILE_HEADERS)
    media_id = payload.get("media_id")
    if not isinstance(media_id, (str, int)) or isinstance(media_id, bool):
        raise PublicMetadataError("oembed_no_media_id")
    media_id = str(media_id)
    if not re.fullmatch(r"[0-9]{1,30}(?:_[0-9]{1,30})?", media_id):
        raise PublicMetadataError("oembed_no_media_id")
    return media_id


async def mobile_info(session, media_id, shortcode):
    payload = await public_json(session,
        f"https://i.instagram.com/api/v1/media/{media_id}/info/", "mobile_info",
        headers=MOBILE_HEADERS)
    items = payload.get("items")
    if not isinstance(items, list) or not items or not isinstance(items[0], dict):
        raise PublicMetadataError("mobile_info_empty")
    item = items[0]
    # Some mobile responses omit code; in that case require matching numeric PK.
    if not item.get("code"):
        if str(item.get("pk")) != media_id.split("_")[0]:
            raise PublicMetadataError("mobile_info_identity")
        item = {**item, "code": shortcode}
    return video_info({"data": {"xdt_api__v1__media__shortcode__web_info":
                              {"items": [item]}}}, shortcode)


def embed_info(html, shortcode):
    if len(html) > 5_000_000:
        raise PublicMetadataError("embed_parse_failed")
    decoder = json.JSONDecoder()
    # Parse JSON values, rather than a brace regex (captions can contain braces).
    for match in list(re.finditer(r'"init",\s*\[\s*\],\s*\[', html))[:20]:
        try:
            init, _ = decoder.raw_decode(html[match.end():].lstrip())
            context = init.get("contextJSON") if isinstance(init, dict) else None
            context = json.loads(context) if isinstance(context, str) else context
        except (ValueError, TypeError):
            continue
        if not isinstance(context, dict):
            continue
        payload = {"data": context.get("gql_data")} if "gql_data" in context else {
            "data": {"xdt_api__v1__media__shortcode__web_info": {"items": [context]}}}
        try:
            return video_info(payload, shortcode)
        except PublicMetadataError as error:
            if str(error) in {"metadata_no_video", "metadata_items_missing",
                              "metadata_schema_unknown", "graphql_data_missing"}:
                raise PublicMetadataError("embed_no_video") from None
            raise
    raise PublicMetadataError("embed_parse_failed")


def shortcode_media_id(shortcode):
    """Instagram shortcodes encode the media PK as a URL-safe base-64 integer."""
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,28}", shortcode):
        raise PublicMetadataError("shortcode_id_invalid")
    media_id = 0
    # Extended links append extra information after the 11-character media code.
    for char in shortcode[:11]:
        media_id = media_id * 64 + alphabet.index(char)
    if not media_id:
        raise PublicMetadataError("shortcode_id_invalid")
    return str(media_id)


async def public_embed(session, shortcode):
    url = f"https://www.instagram.com/p/{shortcode}/embed/captioned/"
    for attempt in range(3):
        response = await session.get(url,
            headers={"Accept": "text/html", "Accept-Language": "en-US,en;q=0.9"},
            timeout=REQUEST_TIMEOUT)
        if response.status_code not in {301, 302, 303, 307, 308}:
            if response.status_code != 200:
                raise PublicMetadataError(f"embed_http_{response.status_code}")
            return embed_info(response.text, shortcode)
        location = response.headers.get("Location", "")
        try:
            target = urljoin(url, location)
            parsed = urlsplit(target)
            allowed_origin = (location and parsed.scheme == "https"
                and parsed.hostname == "www.instagram.com" and not parsed.username
                and not parsed.password and parsed.port in {None, 443})
        except ValueError:
            allowed_origin = False
        if not allowed_origin:
            raise PublicMetadataError("embed_redirect_disallowed")
        if parsed.path.startswith("/accounts/login"):
            raise PublicMetadataError("embed_redirect_login")
        if parsed.path.startswith(("/challenge", "/accounts/suspended")):
            raise PublicMetadataError("embed_redirect_challenge")
        if not re.fullmatch(r"/(?:p|reel|reels|tv)/" + re.escape(shortcode)
                            + r"(?:/embed(?:/captioned)?)?/?", parsed.path):
            raise PublicMetadataError("embed_redirect_path_disallowed")
        logger.info("instagram_embed_redirect", status=response.status_code,
                    target_kind="embed" if "/embed" in parsed.path else "post")
        if attempt == 2:
            raise PublicMetadataError("embed_redirect_limit")
        url = target


def dash_duration(manifest):
    """Read only the MPD duration attribute; never fetch manifest URLs."""
    if not isinstance(manifest, str) or len(manifest) > 1_000_000:
        return None
    number = r"(\d{1,9}(?:\.\d{1,9})?)"
    match = re.search(r"\bmediaPresentationDuration\s*=\s*['\"]PT(?:"
                      + number + r"H)?(?:" + number + r"M)?(?:" + number + r"S)?['\"]", manifest)
    if not match or not any(match.groups()):
        return None
    duration = sum(float(value or 0) * unit for value, unit in zip(match.groups(), (3600, 60, 1)))
    return duration if duration > 0 else None


def post_html_info(html, shortcode):
    """Read the public media node rendered into the logged-out post page."""
    if len(html) > 5_000_000:
        raise PublicMetadataError("post_html_parse_failed")
    match = re.search(r'"xig_polaris_media"\s*:\s*', html)
    if not match:
        raise PublicMetadataError("post_html_media_missing")
    try:
        wrapper, _ = json.JSONDecoder().raw_decode(html[match.end():])
    except ValueError:
        raise PublicMetadataError("post_html_parse_failed") from None
    if not isinstance(wrapper, dict):
        raise PublicMetadataError("post_html_invalid_media")
    if "if_not_gated_logged_out" in wrapper:
        item = wrapper["if_not_gated_logged_out"]
        if not isinstance(item, dict):
            raise PublicMetadataError("post_html_gated")
    else:
        item = wrapper
    if not item.get("video_versions"):
        raise PublicMetadataError("post_html_no_video")
    if not item.get("video_duration"):
        item = {**item, "video_duration": dash_duration(item.get("video_dash_manifest"))}
    return video_info({"data": {"xdt_api__v1__media__shortcode__web_info":
                              {"items": [item]}}}, shortcode)


async def public_post_html(session, shortcode):
    response = await session.get(f"https://www.instagram.com/p/{shortcode}/",
        headers={"User-Agent": "Googlebot/2.1 (+http://www.google.com/bot.html)",
                 "Accept": "text/html", "Accept-Language": "en-US,en;q=0.9"},
        timeout=REQUEST_TIMEOUT)
    if response.status_code in {301, 302, 303, 307, 308}:
        # Diagnose only; this stage never follows a redirect or logs Location.
        try:
            target = urlsplit(urljoin(f"https://www.instagram.com/p/{shortcode}/",
                                     response.headers.get("Location", "")))
        except ValueError:
            target = None
        if (target and target.hostname == "www.instagram.com"
                and target.path.startswith("/accounts/login")):
            raise PublicMetadataError("post_html_redirect_login")
    if response.status_code != 200:
        raise PublicMetadataError(f"post_html_http_{response.status_code}")
    return post_html_info(response.text, shortcode)


async def public_graphql(session, shortcode):
    home = await public_homepage(session)
    token = re.search(r'\["LSD",\[\],\{"token":"([^"]+)"', home.text)
    if not token:
        raise PublicMetadataError("homepage_token_missing")
    headers = {
        "X-FB-LSD": token[1], "X-IG-App-ID": "936619743392459",
        "X-FB-Friendly-Name": "PolarisPostRootQuery", "Origin": "https://www.instagram.com",
        "Referer": f"https://www.instagram.com/p/{shortcode}/",
        "Content-Type": "application/x-www-form-urlencoded", "Accept": "*/*",
    }
    # Bind CSRF header to the cookie issued for this anonymous session.
    for cookie in session.cookies.jar:
        if cookie.name == "csrftoken" and cookie.domain.lstrip(".") in {"instagram.com", "www.instagram.com"}:
            headers["X-CSRFToken"] = cookie.value
            break
    body = {"lsd": token[1], "doc_id": DOC_ID, "server_timestamps": "true",
            "variables": json.dumps({"shortcode": shortcode,
                "__relay_internal__pv__PolarisShortDramaEnabledrelayprovider": False,
                "__relay_internal__pv__PolarisMultiCaptionCarouselEnabledrelayprovider": True})}
    dtsg = re.search(r'\["DTSG(?:Init|Initial)Data",\[\],\{"token":"([^"]+)"', home.text)
    if dtsg:
        body["fb_dtsg"] = dtsg[1]
    response = await session.post("https://www.instagram.com/api/graphql", headers=headers, data=body, timeout=REQUEST_TIMEOUT)
    if response.status_code != 200:
        raise PublicMetadataError(f"graphql_http_{response.status_code}")
    try:
        payload = response.json()
    except ValueError:
        raise PublicMetadataError("graphql_not_json") from None
    if not isinstance(payload, dict):
        raise PublicMetadataError("graphql_invalid_payload")
    return video_info(payload, shortcode)


async def public_stage(stage, operation):
    logger.info("instagram_public_stage_start", stage=stage)
    try:
        # Total wall-clock bound, including redirects/parsing in a stage.
        result = await asyncio.wait_for(operation(), timeout=30 if stage == "graphql" else 12)
    except PublicMetadataError as error:
        logger.warning("instagram_public_stage_failed", stage=stage, reason=str(error))
        raise
    except Exception as error:
        reason = f"{stage}_timeout" if isinstance(error, asyncio.TimeoutError) else f"{stage}_transport_error"
        logger.warning("instagram_public_stage_failed", stage=stage, reason=reason)
        raise PublicMetadataError(reason) from None
    logger.info("instagram_public_stage_ok", stage=stage)
    return result


async def extract_public_video(url: str) -> dict:
    parsed = urlsplit(url)
    match = re.fullmatch(r"/(?:reel|reels|p|tv)/([A-Za-z0-9_-]{1,28})/?", parsed.path)
    if (not match or parsed.scheme != "https" or parsed.hostname not in
            {"instagram.com", "www.instagram.com", "instagr.am"}
            or parsed.username or parsed.port not in {None, 443}):
        raise PublicMetadataError("unsupported_post")
    shortcode = match[1]
    from app.pipeline.instagram_proxy import instagram_proxy_url, InstagramProxyConfigError
    try:
        proxy = instagram_proxy_url()
    except InstagramProxyConfigError:
        raise PublicMetadataError("instagram_proxy_config_invalid") from None
    async with AsyncSession(impersonate="chrome", timeout=REQUEST_TIMEOUT,
                            allow_redirects=False, **({"proxy": proxy} if proxy else {})) as session:
        try:
            media_id = await public_stage("oembed", lambda: public_oembed(session, shortcode))
        except PublicMetadataError:
            # oEmbed authentication must not prevent the independent mobile attempt.
            media_id = shortcode_media_id(shortcode)
            logger.info("instagram_public_media_id", source="shortcode")
        try:
            return await public_stage("mobile_info", lambda: mobile_info(session, media_id, shortcode))
        except PublicMetadataError:
            pass
        try:
            return await public_stage("embed", lambda: public_embed(session, shortcode))
        except PublicMetadataError:
            pass
        try:
            return await public_stage("post_html", lambda: public_post_html(session, shortcode))
        except PublicMetadataError:
            pass
        return await public_stage("graphql", lambda: public_graphql(session, shortcode))
