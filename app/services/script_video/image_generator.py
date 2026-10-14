"""ImageGenerator — parallel image generation with retry + fallback.

Uses OpenRouter /images/generations endpoint.
Target cost: ~$0.00003 per image (gemini-nano-banana-2.1).
Concurrency capped by settings (default 2) to avoid rate limits.
"""
from __future__ import annotations

import asyncio
from typing import Optional

from app.core.config import get_settings
from app.core.logging import get_logger
from app.services.script_video.openrouter_client import OpenRouterClient
from app.services.script_video.cost_config import PRICE_MAP

logger = get_logger(__name__)


class ImageGenerationError(RuntimeError):
    pass


class ImageGenerator:
    def __init__(
        self,
        model: Optional[str] = None,
        fallback_model: Optional[str] = None,
        concurrency: Optional[int] = None,
        max_retries: int = 2,
        client: Optional[OpenRouterClient] = None,
    ) -> None:
        s = get_settings()
        self.model = model or s.script_video_image_model
        self.fallback_model = fallback_model or s.script_video_image_fallback_model
        self.concurrency = concurrency or s.script_video_image_concurrency
        self.max_retries = max_retries
        self.client = client or OpenRouterClient()

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
        results: list[Optional[list[str]]] = [None] * len(prompts)

        async def _generate_single(idx: int, prompt: str) -> None:
            async with sem:
                for attempt in range(self.max_retries):
                    try:
                        async with self.client as c:
                            model_to_use = self.model
                            urls = await c.generate_image(
                                model=model_to_use,
                                prompt=prompt,
                                size=size,
                                n=n_per_prompt,
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
                        logger.warning(
                            "image_retry",
                            prompt_preview=prompt[:60],
                            attempt=attempt + 1,
                            exc=str(exc)[:200],
                        )
                        # On last retry, try fallback model
                        if attempt == self.max_retries - 1:
                            try:
                                async with self.client as c:
                                    urls = await c.generate_image(
                                        model=self.fallback_model,
                                        prompt=prompt,
                                        size=size,
                                        n=n_per_prompt,
                                    )
                                    results[idx] = urls
                                    logger.info(
                                        "image_fallback",
                                        prompt_preview=prompt[:60],
                                        model=self.fallback_model,
                                    )
                                    return
                            except Exception as fallback_exc:
                                logger.error(
                                    "image_failed",
                                    prompt_preview=prompt[:60],
                                    exc=str(fallback_exc)[:200],
                                )
                                results[idx] = []
                                return

        await asyncio.gather(
            *[_generate_single(i, p) for i, p in enumerate(prompts)]
        )
        # Flatten
        flat: list[str] = []
        for r in results:
            if r:
                flat.extend(r)
        return flat
