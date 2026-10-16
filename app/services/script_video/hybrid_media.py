"""Resolve one visual asset per planned scene using stock video + AI keyframes."""
from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from pathlib import Path

import httpx

from app.core.config import get_settings
from app.core.logging import get_logger
from app.services.script_video.image_generator import ImageGenerator
from app.services.script_video.models import VideoPlan
from app.services.script_video.stock_video import (
    CoverrStockVideoProvider,
    PexelsStockVideoProvider,
    StockVideoAsset,
    StockVideoProviderChain,
)

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class HybridMediaResult:
    paths: tuple[Path, ...]
    stock_assets: tuple[StockVideoAsset, ...]
    ai_images: int
    stock_videos: int
    reused_scenes: int

    @property
    def attribution_text(self) -> str:
        if not self.stock_assets:
            return ""
        providers = {asset.provider for asset in self.stock_assets}
        parts: list[str] = []
        if "coverr" in providers:
            parts.append("Coverr: https://coverr.co")
        if "pexels" in providers:
            creators: list[str] = []
            for asset in self.stock_assets:
                if asset.provider == "pexels" and asset.creator_name not in creators:
                    creators.append(asset.creator_name)
            credit = ", ".join(creators[:2])
            suffix = f" ({credit})" if credit else ""
            parts.append(f"Pexels{suffix}: https://www.pexels.com")
        return " · ".join(parts)


class HybridMediaResolver:
    """Use real B-roll for generic scenes and pay for only a few AI keyframes.

    If no stock provider key is configured, the resolver deliberately preserves
    the current all-AI behavior instead of silently degrading visual quality.
    """

    def __init__(
        self,
        *,
        image_model: str,
        fallback_image_model: str,
        cost_tracker: object | None = None,
        max_ai_images: int = 4,
        max_stock_download_mb: int = 80,
    ) -> None:
        settings = get_settings()
        self.image_model = image_model
        self.fallback_image_model = fallback_image_model
        self.cost_tracker = cost_tracker
        self.max_ai_images = max(1, max_ai_images)
        max_bytes = max(10, max_stock_download_mb) * 1024 * 1024
        coverr_key = os.getenv("COVERR_API_KEY", "").strip()
        pexels_key = os.getenv("PEXELS_API_KEY", "").strip()
        self.stock = StockVideoProviderChain((
            CoverrStockVideoProvider(coverr_key, max_download_bytes=max_bytes),
            PexelsStockVideoProvider(pexels_key, max_download_bytes=max_bytes),
        ))
        self.stock_enabled = (
            os.getenv("SCRIPT_VIDEO_HYBRID_MEDIA_ENABLED", "true").strip().lower()
            not in {"0", "false", "no", "off"}
            and self.stock.enabled
        )
        self.image_concurrency = settings.script_video_image_concurrency

    @staticmethod
    def _ai_priority(plan: VideoPlan, index: int) -> tuple[int, int, int]:
        scene = plan.scenes[index]
        specific = 1 if not scene.prefer_stock else 0
        continuity = 1 if scene.reference_scene_id else 0
        edge = 1 if index in {0, len(plan.scenes) - 1} else 0
        return (
            specific * 10 + continuity * 4 + edge * 2 + scene.importance,
            scene.importance,
            -index,
        )

    @staticmethod
    async def _save_generated_image(item: str, path_stem: Path) -> Path:
        if item.startswith("data:"):
            header, encoded = item.split(",", 1)
            image_bytes = base64.b64decode(encoded, validate=True)
            ext = ".jpg" if "jpeg" in header else ".png"
        else:
            async with httpx.AsyncClient(timeout=45, follow_redirects=True) as client:
                response = await client.get(item)
                response.raise_for_status()
                image_bytes = response.content
                content_type = response.headers.get("content-type", "")
                ext = ".jpg" if "jpeg" in content_type else ".png"
        if not image_bytes:
            raise RuntimeError("generated image is empty")
        if len(image_bytes) > 15 * 1024 * 1024:
            raise RuntimeError("generated image exceeds size limit")
        path = path_stem.with_suffix(ext)
        path.write_bytes(image_bytes)
        return path

    @staticmethod
    def _nearest_path(paths: list[Path | None], index: int) -> Path | None:
        candidates = [i for i, path in enumerate(paths) if path is not None]
        if not candidates:
            return None
        nearest = min(candidates, key=lambda candidate: abs(candidate - index))
        return paths[nearest]

    async def resolve(self, plan: VideoPlan, workdir: Path) -> HybridMediaResult:
        scene_count = len(plan.scenes)
        if scene_count < 1:
            raise RuntimeError("video plan has no scenes")

        paths: list[Path | None] = [None] * scene_count
        stock_assets: list[StockVideoAsset] = []
        used_stock_ids: set[str] = set()

        if self.stock_enabled:
            for index, scene in enumerate(plan.scenes):
                if not scene.prefer_stock:
                    continue
                asset = await self.stock.fetch(
                    query=scene.stock_query or scene.visual_prompt[:80],
                    workdir=workdir,
                    slot=index,
                    target_seconds=scene.duration_hint,
                    used_ids=used_stock_ids,
                )
                if asset is None:
                    continue
                paths[index] = asset.path
                stock_assets.append(asset)
                used_stock_ids.add(asset.unique_id)

        unresolved = [index for index, path in enumerate(paths) if path is None]
        if not unresolved:
            return HybridMediaResult(
                paths=tuple(path for path in paths if path is not None),
                stock_assets=tuple(stock_assets),
                ai_images=0,
                stock_videos=len(stock_assets),
                reused_scenes=0,
            )

        # Without a configured stock API we keep the known-good V2 behavior.
        # With stock enabled, enforce a hard AI budget and reuse a nearby visual
        # only if a stock search failed after the paid keyframes are allocated.
        ai_budget = (
            len(unresolved)
            if not self.stock_enabled
            else min(self.max_ai_images, len(unresolved))
        )
        ai_indices = sorted(
            unresolved,
            key=lambda index: self._ai_priority(plan, index),
            reverse=True,
        )[:ai_budget]
        ai_indices.sort()

        prompts = [plan.scenes[index].visual_prompt for index in ai_indices]
        generated = await ImageGenerator(
            model=self.image_model,
            fallback_model=self.fallback_image_model,
            concurrency=self.image_concurrency,
            cost_tracker=self.cost_tracker,
        ).generate(
            prompts,
            aspect_ratio="9:16",
            max_images=len(prompts),
        )
        if not generated:
            raise RuntimeError("image generation returned no images")

        # One image per prompt is requested. ImageGenerator already fills isolated
        # provider failures with the nearest successful result, preserving length.
        for position, scene_index in enumerate(ai_indices):
            source = generated[min(position, len(generated) - 1)]
            paths[scene_index] = await self._save_generated_image(
                source,
                workdir / f"scene-{scene_index:02d}-ai",
            )

        reused = 0
        for index, path in enumerate(paths):
            if path is not None:
                continue
            nearest = self._nearest_path(paths, index)
            if nearest is None:
                raise RuntimeError("hybrid media resolver produced no fallback asset")
            paths[index] = nearest
            reused += 1
            logger.info(
                "script_media_reused",
                scene_index=index,
                source=nearest.name,
            )

        resolved = tuple(path for path in paths if path is not None)
        if len(resolved) != scene_count:
            raise RuntimeError("hybrid media resolver did not cover every scene")
        logger.info(
            "script_hybrid_media_ready",
            scenes=scene_count,
            stock_videos=len(stock_assets),
            ai_images=len(ai_indices),
            reused_scenes=reused,
            stock_enabled=self.stock_enabled,
        )
        return HybridMediaResult(
            paths=resolved,
            stock_assets=tuple(stock_assets),
            ai_images=len(ai_indices),
            stock_videos=len(stock_assets),
            reused_scenes=reused,
        )
