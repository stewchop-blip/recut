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
        logger.info("script_tts_completed", model=self.model, bytes=len(audio))
        return output_path
