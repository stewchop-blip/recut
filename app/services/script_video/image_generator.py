"""ImageGenerator — parallel image generation with bounded retry + fallback.

Uses OpenRouter /images endpoint. Concurrency is capped by settings to avoid
provider rate limits. Generated script-video assets default to native 9:16 so
composition is created for the final canvas instead of cropped from a square.
"""
from __future__ import annotations

import asyncio
from typing import Optional

from app.core.config import get_settings
from app.core.logging import get_logger
from app.services.script_video.openrouter_client import OpenRouterClient

logger = get_logger(__name__)


class ImageGenerationError(RuntimeError):
    pass


class ImageCreditsExhaustedError(ImageGenerationError):
    """OpenRouter refused image generation because the account has no credits."""


class ImageGenerator:
    def __init__(
        self,
        model: Optional[str] = None,
        fallback_model: Optional[str] = None,
        concurrency: Optional[int] = None,
        max_retries: int = 2,
        client: Optional[OpenRouterClient] = None,
        cost_tracker: Optional[object] = None,
    ) -> None:
        s = get_settings()
        self.model = model or s.script_video_image_model
        # None means "use configured fallback". Empty string explicitly disables
        # a second paid provider, which is what the cost-capped V3 path uses.
        configured_fallback = (
            s.script_video_image_fallback_model
            if fallback_model is None
            else fallback_model
        )

        # Never silently jump from cheap Recraft to Nano Banana. Re-enabling an
        # expensive fallback must be an explicit product decision.
        if (
            self.model.startswith("recraft/recraft-v4.1-flash")
            and configured_fallback
            and "nano-banana" in configured_fallback
        ):
            logger.warning(
                "expensive_image_fallback_disabled",
                primary_model=self.model,
                fallback_model=configured_fallback,
            )
            configured_fallback = ""

        self.fallback_model = (
            configured_fallback
            if configured_fallback
            and configured_fallback != "openrouter/auto"
            and configured_fallback != self.model
            else ""
        )
        self.concurrency = concurrency or s.script_video_image_concurrency
        self.max_retries = max(1, min(3, max_retries))
        self.client = client or OpenRouterClient()
        self.cost_tracker = cost_tracker
        self.default_aspect_ratio = getattr(s, "script_video_image_aspect_ratio", "9:16")
        self.default_resolution = getattr(s, "script_video_image_resolution", "")

    def _record_actual_cost(self, component: str, cost: float) -> None:
        add_cost = getattr(self.cost_tracker, "add_actual_cost", None)
        if callable(add_cost) and isinstance(cost, (int, float)):
            add_cost(component, float(cost))

    async def generate(
        self,
        prompts: list[str],
        size: str | None = None,
        n_per_prompt: int = 1,
        max_images: Optional[int] = None,
        aspect_ratio: str | None = None,
        resolution: str | None = None,
        preserve_slots: bool = False,
    ) -> list[str]:
        """Generate images for multiple prompts concurrently.

        Returns a flat list of image URLs / base64 strings. A compatible image
        model receives a native vertical aspect ratio by default. preserve_slots
        leaves failed anchors empty for the chronological story fallback.
        """
        s = get_settings()
        image_limit = max_images if max_images is not None else s.script_video_max_images_pro
        if image_limit <= 0 or not prompts:
            return []
        prompts = prompts[:image_limit]
        aspect_ratio = aspect_ratio or self.default_aspect_ratio
        resolution = resolution or self.default_resolution or None
        sem = asyncio.Semaphore(self.concurrency)
        credits_exhausted = asyncio.Event()
        results: list[Optional[list[str]]] = [None] * len(prompts)

        def _is_insufficient_credits(exc: Exception) -> bool:
            response = getattr(exc, "response", None)
            return getattr(response, "status_code", None) == 402

        async def _generate_single(idx: int, prompt: str) -> None:
            async with sem:
                if credits_exhausted.is_set():
                    return
                for attempt in range(self.max_retries):
                    if credits_exhausted.is_set():
                        return
                    try:
                        urls = await client.generate_image(
                            model=self.model,
                            prompt=prompt,
                            size=size,
                            aspect_ratio=aspect_ratio,
                            resolution=resolution,
                            n=n_per_prompt,
                            on_actual_cost=lambda cost: self._record_actual_cost("image", cost),
                        )
                        results[idx] = urls
                        logger.info(
                            "image_generated",
                            prompt_preview=prompt[:60],
                            model=self.model,
                            count=len(urls),
                            aspect_ratio=aspect_ratio,
                            resolution=resolution,
                        )
                        return
                    except Exception as exc:
                        if _is_insufficient_credits(exc):
                            credits_exhausted.set()
                            logger.error("image_generation_credits_exhausted", model=self.model)
                            results[idx] = []
                            return
                        if credits_exhausted.is_set():
                            results[idx] = []
                            return
                        logger.warning(
                            "image_retry",
                            prompt_preview=prompt[:60],
                            attempt=attempt + 1,
                            exc=str(exc)[:200],
                        )
                        if attempt == self.max_retries - 1 and self.fallback_model:
                            try:
                                urls = await client.generate_image(
                                    model=self.fallback_model,
                                    prompt=prompt,
                                    size=size,
                                    aspect_ratio=aspect_ratio,
                                    resolution=resolution,
                                    n=n_per_prompt,
                                    on_actual_cost=lambda cost: self._record_actual_cost(
                                        "image_fallback", cost
                                    ),
                                )
                                results[idx] = urls
                                logger.info(
                                    "image_fallback",
                                    prompt_preview=prompt[:60],
                                    model=self.fallback_model,
                                    aspect_ratio=aspect_ratio,
                                )
                                return
                            except Exception as fallback_exc:
                                if _is_insufficient_credits(fallback_exc):
                                    credits_exhausted.set()
                                    logger.error(
                                        "image_generation_credits_exhausted",
                                        model=self.fallback_model,
                                    )
                                    results[idx] = []
                                    return
                                logger.error(
                                    "image_failed",
                                    prompt_preview=prompt[:60],
                                    exc=str(fallback_exc)[:200],
                                )
                                results[idx] = []
                                return

        async with self.client as client:
            await asyncio.gather(
                *[_generate_single(i, p) for i, p in enumerate(prompts)]
            )
        successful = [index for index, result in enumerate(results) if result]
        if not successful and credits_exhausted.is_set():
            raise ImageCreditsExhaustedError(
                "OpenRouter image generation credits are exhausted"
            )
        if successful and not preserve_slots:
            for index, result in enumerate(results):
                if result:
                    continue
                nearest = min(successful, key=lambda candidate: abs(candidate - index))
                results[index] = results[nearest]
                logger.info(
                    "image_asset_reused",
                    failed_scene_index=index,
                    source_scene_index=nearest,
                )

        flat: list[str] = []
        for result in results:
            if result:
                flat.extend(result)
            elif preserve_slots:
                flat.append("")
        return flat
