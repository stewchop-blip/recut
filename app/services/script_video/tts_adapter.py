"""OpenRouter text-to-speech adapter for generated video narration."""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

import httpx

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


class ScriptTTSAdapter:
    def __init__(
        self,
        model: str | None = None,
        voice: str | None = None,
        fallback_model: str | None = None,
        fallback_voice: str | None = None,
    ) -> None:
        settings = get_settings()
        self.model = model or settings.script_video_tts_model
        self.voice = voice or getattr(settings, "script_video_tts_voice", "george")
        if fallback_model is None:
            self.fallback_model = getattr(
                settings,
                "script_video_tts_fallback_model",
                "openai/gpt-4o-mini-tts-2025-12-15",
            )
        else:
            self.fallback_model = fallback_model
        self.fallback_voice = fallback_voice or getattr(
            settings,
            "script_video_tts_fallback_voice",
            "nova",
        )
        self.api_key = settings.openrouter_api_key
        self.actual_cost_usd = 0.0

    @staticmethod
    def _clean_text(text: str) -> str:
        cleaned = re.sub(r"[`*_#]+", "", text)
        cleaned = re.sub(r"[ \t]+", " ", cleaned)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        return cleaned.strip()

    def _prepare_text(self, text: str, model: str) -> str:
        cleaned = self._clean_text(text)
        if model.startswith("elevenlabs/eleven-v4"):
            return f"[confident] {cleaned}"
        return cleaned

    @staticmethod
    def _response_cost(response: httpx.Response) -> float | None:
        for name in ("x-openrouter-cost", "x-usage-cost", "x-request-cost"):
            raw = response.headers.get(name)
            if raw is None:
                continue
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            if value >= 0:
                return value
        return None

    async def _generation_cost(
        self,
        client: httpx.AsyncClient,
        generation_id: str,
    ) -> float | None:
        """Resolve exact TTS dollar spend from OpenRouter generation metadata."""
        if not generation_id:
            return None
        headers = {"Authorization": f"Bearer {self.api_key}"}
        for attempt in range(2):
            if attempt:
                await asyncio.sleep(0.25)
            try:
                response = await client.get(
                    "https://openrouter.ai/api/v1/generation",
                    headers=headers,
                    params={"id": generation_id},
                    timeout=10,
                )
                if response.status_code == 404:
                    continue
                response.raise_for_status()
                data = (response.json() or {}).get("data") or {}
                # `usage` can represent token/unit consumption and must never be
                # interpreted as USD. Only explicit cost fields are accepted.
                for key in ("total_cost", "upstream_inference_cost"):
                    value = data.get(key)
                    if isinstance(value, (int, float)) and value >= 0:
                        return float(value)
            except Exception:
                break
        return None

    async def _synthesize_openrouter(
        self,
        *,
        client: httpx.AsyncClient,
        text: str,
        output_path: Path,
        model: str,
        voice: str,
    ) -> Path:
        payload = {
            "model": model,
            "input": self._prepare_text(text, model)[:10000],
            "voice": voice,
            "response_format": "mp3",
        }
        response = await client.post(
            "https://openrouter.ai/api/v1/audio/speech",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/stewchop-blip/recut",
            },
            json=payload,
        )
        response.raise_for_status()
        audio = response.content
        content_type = response.headers.get("content-type", "")
        if not audio or not content_type.startswith("audio/"):
            raise RuntimeError("TTS provider returned no audio")
        output_path.write_bytes(audio)

        actual_cost = self._response_cost(response)
        generation_id = response.headers.get("x-generation-id", "")
        if actual_cost is None and generation_id:
            actual_cost = await self._generation_cost(client, generation_id)
        if actual_cost is not None:
            self.actual_cost_usd += actual_cost
        logger.info(
            "script_tts_completed",
            model=model,
            provider="openrouter",
            voice=voice,
            bytes=len(audio),
            generation_id=generation_id or None,
            actual_cost_usd=actual_cost,
        )
        return output_path

    async def synthesize_narration(self, text: str, output_path: Path) -> Path:
        if not text.strip():
            raise ValueError("Narration text is empty")
        if not self.api_key:
            raise RuntimeError("OpenRouter API key is not configured")
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        primary_error: Exception | None = None
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                return await self._synthesize_openrouter(
                    client=client,
                    text=text,
                    output_path=output_path,
                    model=self.model,
                    voice=self.voice,
                )
        except (httpx.HTTPError, RuntimeError) as exc:
            primary_error = exc
            logger.warning(
                "script_tts_primary_failed",
                model=self.model,
                status_code=getattr(getattr(exc, "response", None), "status_code", None),
                error_type=type(exc).__name__,
            )

        if self.fallback_model and self.fallback_model != self.model:
            try:
                async with httpx.AsyncClient(timeout=120) as client:
                    return await self._synthesize_openrouter(
                        client=client,
                        text=text,
                        output_path=output_path,
                        model=self.fallback_model,
                        voice=self.fallback_voice,
                    )
            except (httpx.HTTPError, RuntimeError) as exc:
                logger.warning(
                    "script_tts_openrouter_fallback_failed",
                    model=self.fallback_model,
                    status_code=getattr(getattr(exc, "response", None), "status_code", None),
                    error_type=type(exc).__name__,
                )

        try:
            import edge_tts

            await edge_tts.Communicate(self._clean_text(text)[:4000], "ru-RU-DmitryNeural").save(
                str(output_path)
            )
            audio = output_path.read_bytes()
            if not audio:
                raise RuntimeError("Edge TTS returned no audio")
            logger.info(
                "script_tts_completed",
                model="edge-tts/ru-RU-DmitryNeural",
                provider="edge_tts",
                bytes=len(audio),
                actual_cost_usd=0.0,
            )
            return output_path
        except Exception as exc:
            logger.exception("script_tts_fallback_failed", error_type=type(exc).__name__)
            raise RuntimeError("All narration providers failed") from (primary_error or exc)
