"""Unified OpenRouter client for script video pipeline.

Reuses the same httpx + /api/v1/chat/completions pattern as
app/services/analysis/openrouter.py.
"""
from __future__ import annotations

import json
import time
from typing import Any, Optional

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
            logger.info(
                "openrouter_request",
                model=model,
                usage=data.get("usage"),
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
        size: str = "1024x1024",
        n: int = 1,
    ) -> list[str]:
        """Return list of image URLs (or base64 data if model supports)."""
        # OpenRouter image generation endpoint: /v1/images/generations
        # For models like google/gemini-nano-banana-2.1.
        if not self._client:
            raise RuntimeError("Client not entered; use async with")
        payload: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "n": n,
            "size": size,
        }
        try:
            resp = await self._client.post(
                f"{self.base_url}/images/generations",
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
            # OpenRouter returns image URLs or base64 data
            results: list[str] = []
            for item in data.get("data", []):
                url = item.get("url")
                b64 = item.get("b64_json")
                if url:
                    results.append(url)
                elif b64:
                    results.append(f"data:image/png;base64,{b64}")
            logger.info("openrouter_image", model=model, count=len(results), prompt_preview=prompt[:80])
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
