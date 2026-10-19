"""Stock-video retrieval for Script Video hybrid rendering.

The hybrid pipeline prefers rights-clean real B-roll when available and falls
back to AI imagery when a provider cannot supply a useful clip.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from app.services.script_video.visual_policy import stock_title_matches_query
from typing import Iterable, Protocol

import httpx

from app.core.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class StockVideoAsset:
    path: Path
    provider: str
    source_url: str
    creator_name: str
    creator_url: str
    asset_id: str
    title: str = ""

    @property
    def unique_id(self) -> str:
        return f"{self.provider}:{self.asset_id}"


class StockVideoProvider(Protocol):
    @property
    def enabled(self) -> bool: ...

    async def fetch(
        self,
        *,
        query: str,
        workdir: Path,
        slot: int,
        target_seconds: float,
        used_ids: Iterable[str] = (),
    ) -> StockVideoAsset | None: ...


async def _download_video(
    client: httpx.AsyncClient,
    *,
    url: str,
    path: Path,
    max_download_bytes: int,
) -> int:
    total = 0
    async with client.stream("GET", url) as response:
        response.raise_for_status()
        content_type = response.headers.get("content-type", "")
        if content_type and "video" not in content_type and "octet-stream" not in content_type:
            raise RuntimeError("stock provider returned non-video content")
        with path.open("wb") as fh:
            async for chunk in response.aiter_bytes(256 * 1024):
                total += len(chunk)
                if total > max_download_bytes:
                    raise RuntimeError("stock video exceeds size limit")
                fh.write(chunk)
    if total < 64 * 1024:
        raise RuntimeError("stock video is unexpectedly small")
    return total


def _strip_html(value: str | None) -> str:
    if not value:
        return ""
    return html.unescape(re.sub(r"<[^>]+>", "", value)).strip()


class WikimediaCommonsStockVideoProvider:
    """Zero-key Wikimedia Commons provider.

    Only CC0 and unambiguously public-domain files are accepted. This keeps the
    automatic path safe for future commercial use without requiring per-clip
    attribution text or a provider account.
    """

    SEARCH_URL = "https://commons.wikimedia.org/w/api.php"
    SAFE_LICENSES = {"cc0", "public domain", "public domain mark"}

    def __init__(
        self,
        *,
        timeout_seconds: float = 35.0,
        max_download_bytes: int = 80 * 1024 * 1024,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.max_download_bytes = max_download_bytes
        self._search_cache: dict[str, list[dict]] = {}

    @property
    def enabled(self) -> bool:
        return True

    @classmethod
    def _license_is_safe(cls, license_name: str) -> bool:
        normalized = " ".join((license_name or "").casefold().split())
        return normalized in cls.SAFE_LICENSES or normalized.startswith("cc0 ")

    @staticmethod
    def _meta_value(metadata: dict, key: str) -> str:
        field = metadata.get(key) or {}
        if isinstance(field, dict):
            return str(field.get("value") or "")
        return ""

    @staticmethod
    def _extension_for_mime(mime: str) -> str:
        lowered = (mime or "").lower()
        if "webm" in lowered:
            return ".webm"
        if "ogg" in lowered or "ogv" in lowered:
            return ".ogv"
        return ".mp4"

    async def _search(self, client: httpx.AsyncClient, query: str) -> list[dict]:
        normalized = " ".join((query or "").lower().split())[:80]
        if not normalized:
            return []
        cached = self._search_cache.get(normalized)
        if cached is not None:
            return cached

        response = await client.get(
            self.SEARCH_URL,
            headers={
                "User-Agent": "ReCut/1.0 (https://github.com/stewchop-blip/recut)",
            },
            params={
                "action": "query",
                "format": "json",
                "formatversion": 2,
                "generator": "search",
                "gsrsearch": f"filetype:video {normalized}",
                "gsrnamespace": 6,
                "gsrlimit": 30,
                "prop": "imageinfo",
                "iiprop": "url|size|mime|extmetadata|metadata",
            },
        )
        response.raise_for_status()
        raw_pages = ((response.json() or {}).get("query") or {}).get("pages") or []
        if isinstance(raw_pages, dict):
            pages = list(raw_pages.values())
        else:
            pages = list(raw_pages)

        candidates: list[dict] = []
        for rank, page in enumerate(pages):
            info_items = page.get("imageinfo") or []
            if not info_items:
                continue
            info = info_items[0] or {}
            mime = str(info.get("mime") or "")
            if not mime.startswith("video/"):
                continue
            metadata = info.get("extmetadata") or {}
            license_name = _strip_html(self._meta_value(metadata, "LicenseShortName"))
            if not self._license_is_safe(license_name):
                continue
            size = int(info.get("size") or 0)
            if size and size > self.max_download_bytes:
                continue
            url = str(info.get("url") or "")
            if not url:
                continue
            candidates.append({
                "id": str(page.get("pageid") or ""),
                "title": str(page.get("title") or "").removeprefix("File:"),
                "url": url,
                "source_url": str(info.get("descriptionurl") or ""),
                "creator": _strip_html(
                    self._meta_value(metadata, "Artist")
                    or self._meta_value(metadata, "Credit")
                ),
                "license": license_name,
                "mime": mime,
                "size": size,
                "width": int(info.get("width") or 0),
                "height": int(info.get("height") or 0),
                "duration": float(info.get("duration") or 0.0),
                "rank": rank,
            })
        self._search_cache[normalized] = candidates
        return candidates

    @staticmethod
    def _rank_video(video: dict, target_seconds: float) -> tuple[float, int, int]:
        width = int(video.get("width") or 0)
        height = int(video.get("height") or 0)
        duration = float(video.get("duration") or 0.0)
        rank = int(video.get("rank") or 0)
        portrait_bonus = 6.0 if height > width and width > 0 else 0.0
        relevance_bonus = max(0.0, 8.0 - rank * 0.35)
        duration_penalty = min(6.0, abs(max(1.0, duration) - max(1.0, target_seconds)) * 0.15)
        resolution = min(max(width, height), 1920)
        return (
            portrait_bonus + relevance_bonus - duration_penalty,
            resolution,
            -int(video.get("size") or 0),
        )

    async def fetch(
        self,
        *,
        query: str,
        workdir: Path,
        slot: int,
        target_seconds: float,
        used_ids: Iterable[str] = (),
    ) -> StockVideoAsset | None:
        if not query.strip():
            return None
        excluded = set(used_ids)
        try:
            timeout = httpx.Timeout(self.timeout_seconds, connect=12.0)
            async with httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=True,
                headers={
                    # Wikimedia requires client identification on actual downloads,
                    # not only on metadata search requests.
                    "User-Agent": "ReCutBot/1.0 (https://github.com/stewchop-blip/recut) httpx",
                },
            ) as client:
                videos = await self._search(client, query)
                candidates = [
                    video for video in videos
                    if f"wikimedia:{video.get('id')}" not in excluded
                ]
                candidates = [
                    video for video in candidates
                    if stock_title_matches_query(query, str(video.get("title") or ""))
                ]
                candidates.sort(key=lambda v: self._rank_video(v, target_seconds), reverse=True)
                for video in candidates[:5]:
                    asset_id = str(video.get("id") or "")
                    link = str(video.get("url") or "")
                    if not asset_id or not link:
                        continue
                    ext = self._extension_for_mime(str(video.get("mime") or ""))
                    path = workdir / f"stock-{slot:02d}-wikimedia-{asset_id}{ext}"
                    try:
                        total = await _download_video(
                            client,
                            url=link,
                            path=path,
                            max_download_bytes=self.max_download_bytes,
                        )
                        asset = StockVideoAsset(
                            path=path,
                            provider="wikimedia",
                            source_url=str(video.get("source_url") or "https://commons.wikimedia.org"),
                            creator_name=str(video.get("creator") or "Wikimedia Commons"),
                            creator_url="https://commons.wikimedia.org",
                            asset_id=asset_id,
                            title=str(video.get("title") or ""),
                        )
                        logger.info(
                            "script_stock_video_ready",
                            provider=asset.provider,
                            asset_id=asset.asset_id,
                            bytes=total,
                            query=query[:80],
                            license=video.get("license"),
                        )
                        return asset
                    except Exception as exc:
                        path.unlink(missing_ok=True)
                        logger.warning(
                            "script_stock_video_download_failed",
                            provider="wikimedia",
                            asset_id=asset_id,
                            error_type=type(exc).__name__,
                            status_code=getattr(getattr(exc, "response", None), "status_code", None),
                        )
                        # A global block or rate limit affects the whole provider;
                        # trying additional CDN URLs wastes time and amplifies it.
                        if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (403, 429):
                            break
                return None
        except Exception as exc:
            logger.warning(
                "script_stock_video_search_failed",
                provider="wikimedia",
                query=query[:80],
                error_type=type(exc).__name__,
                status_code=getattr(getattr(exc, "response", None), "status_code", None),
            )
            return None


class PexelsStockVideoProvider:
    SEARCH_URL = "https://api.pexels.com/v1/videos/search"

    @staticmethod
    def _description(video: dict) -> str:
        # Pexels generally has no video title. Use its public URL slug,
        # and reject opaque numeric slugs.
        slug = Path(urlparse(str(video.get("url") or "")).path).name
        return re.sub(r"-[0-9]+$", "", slug).replace("-", " ").strip()

    def __init__(
        self,
        api_key: str,
        *,
        timeout_seconds: float = 35.0,
        max_download_bytes: int = 80 * 1024 * 1024,
    ) -> None:
        self.api_key = (api_key or "").strip()
        self.timeout_seconds = timeout_seconds
        self.max_download_bytes = max_download_bytes
        self._search_cache: dict[str, list[dict]] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    @staticmethod
    def _file_score(item: dict) -> tuple[int, int, int]:
        if item.get("file_type") != "video/mp4":
            return (-10, 0, 0)
        width = int(item.get("width") or 0)
        height = int(item.get("height") or 0)
        portrait = 1 if height > width else 0
        useful_width = min(width, 1080)
        oversize_penalty = -1 if width > 1440 or height > 2560 else 0
        return (portrait * 10 + oversize_penalty, useful_width, min(height, 1920))

    @classmethod
    def _pick_download(cls, video: dict) -> str:
        files = [f for f in (video.get("video_files") or []) if f.get("link")]
        if not files:
            return ""
        files.sort(key=cls._file_score, reverse=True)
        return str(files[0].get("link") or "")

    async def _search(self, client: httpx.AsyncClient, query: str) -> list[dict]:
        normalized = " ".join((query or "").lower().split())[:80]
        if not normalized:
            return []
        cached = self._search_cache.get(normalized)
        if cached is not None:
            return cached
        response = await client.get(
            self.SEARCH_URL,
            headers={"Authorization": self.api_key},
            params={
                "query": normalized,
                "orientation": "portrait",
                "size": "medium",
                "per_page": 8,
            },
        )
        response.raise_for_status()
        videos = list((response.json() or {}).get("videos") or [])
        self._search_cache[normalized] = videos
        return videos

    @staticmethod
    def _rank_video(video: dict, target_seconds: float) -> tuple[float, int, int]:
        duration = float(video.get("duration") or 0.0)
        width = int(video.get("width") or 0)
        height = int(video.get("height") or 0)
        portrait_bonus = 5.0 if height > width else 0.0
        duration_fit = -abs(max(1.0, target_seconds) - max(1.0, duration))
        return (portrait_bonus + duration_fit, min(width, 1080), min(height, 1920))

    async def fetch(
        self,
        *,
        query: str,
        workdir: Path,
        slot: int,
        target_seconds: float,
        used_ids: Iterable[str] = (),
    ) -> StockVideoAsset | None:
        if not self.enabled or not query.strip():
            return None
        excluded = set(used_ids)
        try:
            timeout = httpx.Timeout(self.timeout_seconds, connect=12.0)
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                videos = await self._search(client, query)
                candidates = [
                    v for v in videos
                    if f"pexels:{v.get('id')}" not in excluded
                ]
                candidates = [
                    video for video in candidates
                    if stock_title_matches_query(query, self._description(video))
                ]
                candidates.sort(key=lambda v: self._rank_video(v, target_seconds), reverse=True)
                for video in candidates[:4]:
                    asset_id = str(video.get("id") or "")
                    link = self._pick_download(video)
                    if not asset_id or not link:
                        continue
                    path = workdir / f"stock-{slot:02d}-pexels-{asset_id}.mp4"
                    try:
                        total = await _download_video(
                            client,
                            url=link,
                            path=path,
                            max_download_bytes=self.max_download_bytes,
                        )
                        user = video.get("user") or {}
                        asset = StockVideoAsset(
                            path=path,
                            provider="pexels",
                            source_url=str(video.get("url") or "https://www.pexels.com"),
                            creator_name=str(user.get("name") or "Pexels creator"),
                            creator_url=str(user.get("url") or "https://www.pexels.com"),
                            asset_id=asset_id,
                            title=self._description(video),
                        )
                        logger.info(
                            "script_stock_video_ready",
                            provider=asset.provider,
                            asset_id=asset.asset_id,
                            bytes=total,
                            query=query[:80],
                        )
                        return asset
                    except Exception as exc:
                        path.unlink(missing_ok=True)
                        logger.warning(
                            "script_stock_video_download_failed",
                            provider="pexels",
                            asset_id=asset_id,
                            error_type=type(exc).__name__,
                        )
                return None
        except Exception as exc:
            logger.warning(
                "script_stock_video_search_failed",
                provider="pexels",
                query=query[:80],
                error_type=type(exc).__name__,
                status_code=getattr(getattr(exc, "response", None), "status_code", None),
            )
            return None


class CoverrStockVideoProvider:
    """Optional Coverr provider using its signed-video URL API."""

    SEARCH_URL = "https://api.coverr.co/videos"

    def __init__(
        self,
        api_key: str,
        *,
        timeout_seconds: float = 35.0,
        max_download_bytes: int = 80 * 1024 * 1024,
    ) -> None:
        self.api_key = (api_key or "").strip()
        self.timeout_seconds = timeout_seconds
        self.max_download_bytes = max_download_bytes
        self._search_cache: dict[str, list[dict]] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    async def _search(self, client: httpx.AsyncClient, query: str) -> list[dict]:
        normalized = " ".join((query or "").lower().split())[:80]
        if not normalized:
            return []
        cached = self._search_cache.get(normalized)
        if cached is not None:
            return cached
        params = {
            "query": normalized,
            "page_size": 10,
            "urls": "true",
            "sort": "popular",
            "filter": "is_vertical:true",
        }
        response = await client.get(
            self.SEARCH_URL,
            headers={"Authorization": f"Bearer {self.api_key}"},
            params=params,
        )
        if response.status_code == 400:
            params.pop("filter", None)
            response = await client.get(
                self.SEARCH_URL,
                headers={"Authorization": f"Bearer {self.api_key}"},
                params=params,
            )
        response.raise_for_status()
        videos = list((response.json() or {}).get("hits") or [])
        self._search_cache[normalized] = videos
        return videos

    @staticmethod
    def _rank_video(video: dict, target_seconds: float) -> tuple[float, int, int]:
        duration = float(video.get("duration") or 0.0)
        vertical = bool(video.get("is_vertical"))
        width = int(video.get("max_width") or 0)
        height = int(video.get("max_height") or 0)
        vertical_bonus = 8.0 if vertical or height > width else 0.0
        duration_fit = -abs(max(1.0, target_seconds) - max(1.0, duration))
        return (vertical_bonus + duration_fit, min(width, 1080), min(height, 1920))

    async def fetch(
        self,
        *,
        query: str,
        workdir: Path,
        slot: int,
        target_seconds: float,
        used_ids: Iterable[str] = (),
    ) -> StockVideoAsset | None:
        if not self.enabled or not query.strip():
            return None
        excluded = set(used_ids)
        try:
            timeout = httpx.Timeout(self.timeout_seconds, connect=12.0)
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                videos = await self._search(client, query)
                candidates = [
                    v for v in videos
                    if f"coverr:{v.get('id')}" not in excluded
                ]
                candidates = [
                    video for video in candidates
                    if stock_title_matches_query(
                        query, str(video.get("title") or video.get("description") or "")
                    )
                ]
                candidates.sort(key=lambda v: self._rank_video(v, target_seconds), reverse=True)
                for video in candidates[:4]:
                    asset_id = str(video.get("id") or "")
                    urls = video.get("urls") or {}
                    link = str(urls.get("mp4_download") or urls.get("mp4") or "")
                    if not asset_id or not link:
                        continue
                    path = workdir / f"stock-{slot:02d}-coverr-{asset_id}.mp4"
                    try:
                        total = await _download_video(
                            client,
                            url=link,
                            path=path,
                            max_download_bytes=self.max_download_bytes,
                        )
                        asset = StockVideoAsset(
                            path=path,
                            provider="coverr",
                            source_url=f"https://coverr.co/search?q={query.replace(' ', '+')}",
                            creator_name="Coverr",
                            creator_url="https://coverr.co",
                            asset_id=asset_id,
                            title=str(video.get("title") or video.get("description") or ""),
                        )
                        logger.info(
                            "script_stock_video_ready",
                            provider=asset.provider,
                            asset_id=asset.asset_id,
                            bytes=total,
                            query=query[:80],
                        )
                        return asset
                    except Exception as exc:
                        path.unlink(missing_ok=True)
                        logger.warning(
                            "script_stock_video_download_failed",
                            provider="coverr",
                            asset_id=asset_id,
                            error_type=type(exc).__name__,
                        )
                return None
        except Exception as exc:
            logger.warning(
                "script_stock_video_search_failed",
                provider="coverr",
                query=query[:80],
                error_type=type(exc).__name__,
                status_code=getattr(getattr(exc, "response", None), "status_code", None),
            )
            return None


class StockVideoProviderChain:
    def __init__(self, providers: Iterable[StockVideoProvider]) -> None:
        self.providers = tuple(provider for provider in providers if provider.enabled)

    @property
    def enabled(self) -> bool:
        return bool(self.providers)

    async def fetch(
        self,
        *,
        query: str,
        workdir: Path,
        slot: int,
        target_seconds: float,
        used_ids: Iterable[str] = (),
    ) -> StockVideoAsset | None:
        for provider in self.providers:
            asset = await provider.fetch(
                query=query,
                workdir=workdir,
                slot=slot,
                target_seconds=target_seconds,
                used_ids=used_ids,
            )
            if asset is not None:
                return asset
        return None
