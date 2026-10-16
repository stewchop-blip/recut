"""OpenRouter text-to-speech adapter for generated video narration."""
from __future__ import annotations

from pathlib import Path

import httpx

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


class ScriptTTSAdapter:
    def __init__(self, model: str | None = None) -> None:
        settings = get_settings()
        self.model = model or settings.script_video_tts_model
        self.api_key = settings.openrouter_api_key

    async def synthesize_narration(self, text: str, output_path: Path) -> Path:
        if not text.strip():
            raise ValueError("Narration text is empty")
        if not self.api_key:
            raise RuntimeError("OpenRouter API key is not configured")
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/stewchop-blip/recut",
        }
        payload = {
            "model": self.model,
            "input": text[:4000],
            "voice": "nova",
            "response_format": "mp3",
        }
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                response = await client.post(
                    "https://openrouter.ai/api/v1/audio/speech",
                    headers=headers,
                    json=payload,
                )
                response.raise_for_status()
                audio = response.content
            if not audio or not response.headers.get("content-type", "").startswith("audio/"):
                raise RuntimeError("TTS provider returned no audio")
            output_path.write_bytes(audio)
            logger.info("script_tts_completed", model=self.model, provider="openrouter", bytes=len(audio))
            return output_path
        except (httpx.HTTPError, RuntimeError) as exc:
            error_code = None
            if isinstance(exc, httpx.HTTPStatusError):
                try:
                    error_code = exc.response.json().get("error", {}).get("code")
                except (ValueError, AttributeError):
                    pass
            # Do not include the provider's error message: it may echo user narration.
            logger.warning(
                "script_tts_openrouter_failed",
                model=self.model,
                status_code=getattr(getattr(exc, "response", None), "status_code", None),
                provider_error_code=error_code,
                error_type=type(exc).__name__,
            )

        try:
            import edge_tts

            await edge_tts.Communicate(text[:4000], "ru-RU-DmitryNeural").save(str(output_path))
            audio = output_path.read_bytes()
            if not audio:
                raise RuntimeError("Edge TTS returned no audio")
            logger.info("script_tts_completed", model="edge-tts/ru-RU-DmitryNeural", provider="edge_tts", bytes=len(audio))
            return output_path
        except Exception as exc:
            logger.exception("script_tts_fallback_failed", error_type=type(exc).__name__)
            raise RuntimeError("Both OpenRouter and Edge TTS narration failed") from exc
