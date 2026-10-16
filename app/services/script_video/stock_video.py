"""Optional stock-video retrieval for Script Video hybrid rendering.

The hybrid pipeline can use one or more user-configured providers. If none is
configured, or a search/download fails, callers transparently fall back to AI
imagery so the existing working pipeline remains available.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
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


class PexelsStockVideoProvider:
    SEARCH_URL = "https://api.pexels.com/v1/videos/search"

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
    """Coverr provider using its signed-video URL API.

    A free/demo application can be used while testing. Attribution requirements
    are handled by returning provider metadata to the Telegram result caption.
    """

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
