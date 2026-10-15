"""ImageGenerator — parallel image generation with bounded retry + fallback.

Uses OpenRouter /images/generations endpoint.
Concurrency capped by settings (default 2) to avoid rate limits.
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
        configured_fallback = fallback_model or s.script_video_image_fallback_model
        self.fallback_model = (
            configured_fallback
            if configured_fallback and configured_fallback != "openrouter/auto"
            else ""
        )
        self.concurrency = concurrency or s.script_video_image_concurrency
        self.max_retries = max_retries
        self.client = client or OpenRouterClient()
        self.cost_tracker = cost_tracker

    def _record_actual_cost(self, component: str, cost: float) -> None:
        add_cost = getattr(self.cost_tracker, "add_actual_cost", None)
        if callable(add_cost) and isinstance(cost, (int, float)):
            add_cost(component, float(cost))

    async def generate(
        self,
        prompts: list[str],
        size: str = "1024x1024",
        n_per_prompt: int = 1,
        max_images: Optional[int] = None,
    ) -> list[str]:
        """Generate images for multiple prompts concurrently.

        Returns flat list of image URLs / base64 strings.
        On failure for a specific prompt, retries with fallback model.
        """
        # Enforce image limits based on user tier (free/pro)
        s = get_settings()
        image_limit = max_images or s.script_video_max_images_pro
        prompts = prompts[:image_limit]
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
                        model_to_use = self.model
                        urls = await client.generate_image(
                            model=model_to_use,
                            prompt=prompt,
                            size=size,
                            n=n_per_prompt,
                            on_actual_cost=lambda cost: self._record_actual_cost("image", cost),
                        )
                        results[idx] = urls
                        logger.info(
                            "image_generated",
                            prompt_preview=prompt[:60],
                            model=model_to_use,
                            count=len(urls),
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
                        # On last retry, try fallback model
                        if attempt == self.max_retries - 1 and self.fallback_model:
                            try:
                                urls = await client.generate_image(
                                    model=self.fallback_model,
                                    prompt=prompt,
                                    size=size,
                                    n=n_per_prompt,
                                    on_actual_cost=lambda cost: self._record_actual_cost("image_fallback", cost),
                                )
                                results[idx] = urls
                                logger.info(
                                    "image_fallback",
                                    prompt_preview=prompt[:60],
                                    model=self.fallback_model,
                                )
                                return
                            except Exception as fallback_exc:
                                if _is_insufficient_credits(fallback_exc):
                                    credits_exhausted.set()
                                    logger.error("image_generation_credits_exhausted", model=self.fallback_model)
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
        # Reuse the nearest successful asset when one scene's provider call fails.
        if successful:
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
        for r in results:
            if r:
                flat.extend(r)
        return flat
