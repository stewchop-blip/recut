"""Unified OpenRouter client for script video pipeline.

Reuses the same httpx + /api/v1/chat/completions pattern as
app/services/analysis/openrouter.py.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

import httpx

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


class OpenRouterClient:
    """Single entry point for all script-video AI calls."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        timeout_seconds: int = 60,
    ) -> None:
        s = get_settings()
        self.api_key = api_key or s.openrouter_api_key
        self.base_url = "https://openrouter.ai/api/v1"
        self.timeout = timeout_seconds
        self._client: Optional[httpx.AsyncClient] = None
        self.last_actual_cost_usd: float | None = None

    async def __aenter__(self) -> OpenRouterClient:
        self._client = httpx.AsyncClient(
            timeout=self.timeout,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/stewchop-blip/recut",
            },
        )
        return self

    async def __aexit__(self, *args: Any) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def chat_completion(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        temperature: float = 0.3,
        max_tokens: int = 2000,
        response_format: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        if not self._client:
            raise RuntimeError("Client not entered; use async with")
        self.last_actual_cost_usd = None
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format:
            payload["response_format"] = response_format
        try:
            resp = await self._client.post(
                f"{self.base_url}/chat/completions",
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
            usage_info = data.get("usage", {})
            actual_cost = usage_info.get("cost") if isinstance(usage_info.get("cost"), (int, float)) else None
            self.last_actual_cost_usd = float(actual_cost) if actual_cost is not None else None
            logger.info(
                "openrouter_request",
                model=model,
                usage=usage_info,
                actual_cost_usd=actual_cost,
            )
            return data
        except httpx.HTTPStatusError as e:
            logger.error(
                "openrouter_http_error",
                model=model,
                status=e.response.status_code,
                body=e.response.text[:500],
            )
            raise
        except Exception as exc:
            logger.error("openrouter_exception", model=model, exc=str(exc))
            raise

    async def generate_image(
        self,
        model: str,
        prompt: str,
        *,
        size: str | None = None,
        aspect_ratio: str | None = None,
        resolution: str | None = None,
        n: int = 1,
        on_actual_cost: Optional[Callable[[float], None]] = None,
    ) -> list[str]:
        """Return image URLs or base64 data from OpenRouter's unified Images API.

        Prefer ``aspect_ratio`` over a square ``size`` for short-form generation.
        OpenRouter exposes native aspect-ratio controls for compatible image models,
        which avoids generating square assets only to crop them into 9:16 later.
        """
        if not self._client:
            raise RuntimeError("Client not entered; use async with")
        self.last_actual_cost_usd = None
        payload: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "n": n,
        }
        if aspect_ratio:
            payload["aspect_ratio"] = aspect_ratio
        elif size:
            payload["size"] = size
        if resolution:
            payload["resolution"] = resolution
        try:
            resp = await self._client.post(
                f"{self.base_url}/images",
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
            usage_info = data.get("usage") or {}
            actual_cost = usage_info.get("cost") if isinstance(usage_info, dict) else None
            self.last_actual_cost_usd = float(actual_cost) if isinstance(actual_cost, (int, float)) else None
            if self.last_actual_cost_usd is not None and on_actual_cost:
                on_actual_cost(self.last_actual_cost_usd)
            results: list[str] = []
            for item in data.get("data", []):
                url = item.get("url")
                b64 = item.get("b64_json")
                if url:
                    results.append(url)
                elif b64:
                    media_type = item.get("media_type") or "image/png"
                    results.append(f"data:{media_type};base64,{b64}")
            logger.info(
                "openrouter_image",
                model=model,
                count=len(results),
                aspect_ratio=aspect_ratio,
                resolution=resolution,
                actual_cost_usd=self.last_actual_cost_usd,
            )
            return results
        except httpx.HTTPStatusError as e:
            logger.error(
                "openrouter_image_http_error",
                model=model,
                status=e.response.status_code,
                body=e.response.text[:500],
            )
            raise
        except Exception as exc:
            logger.error("openrouter_image_exception", model=model, exc=str(exc))
            raise
