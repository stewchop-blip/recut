"""TTS Service - orchestrates TTS providers."""

from typing import Optional

from app.core.config import get_settings
from app.core.logging import get_logger
from app.services.tts.base import AudioResult, TTSError, TTSProvider
from app.services.tts.openrouter import OpenRouterTTSProvider


logger = get_logger(__name__)


class TTSService:
    """High-level TTS service."""

    def __init__(self, provider: Optional[TTSProvider] = None) -> None:
        self._provider = provider or OpenRouterTTSProvider()
        self.settings = get_settings()

    @property
    def provider(self) -> TTSProvider:
        return self._provider

    async def synthesize(
        self,
        text: str,
        voice: str,
        style: Optional[str] = None,
    ) -> AudioResult:
        if len(text) > self.settings.max_text_length:
            raise ValueError(f"Text too long: {len(text)} > {self.settings.max_text_length}")
        if not text.strip():
            raise ValueError("Text cannot be empty")
        if voice not in self._provider.supported_voices:
            raise ValueError(
                f"Unsupported voice: {voice}. "
                f"Available: {list(self._provider.supported_voices.keys())}"
            )
        logger.info("tts_synthesize_start", provider=self._provider.name, voice=voice, text_length=len(text))
        try:
            result = await self._provider.synthesize(text, voice, style)
            logger.info("tts_synthesize_done", provider=self._provider.name, voice=voice, size_bytes=result.size_bytes)
            return result
        except TTSError:
            raise
        except Exception as e:
            logger.error("tts_unexpected_error", provider=self._provider.name, error=str(e))
            raise TTSError(
                message=f"Unexpected TTS error: {e}",
                code="UNEXPECTED_ERROR",
                provider=self._provider.name,
                retryable=False,
            ) from e

    async def close(self) -> None:
        if hasattr(self._provider, "close"):
            await self._provider.close()

    def get_available_voices(self) -> list[str]:
        return list(self._provider.supported_voices.keys())

    def get_voice_label(self, voice_key: str) -> str:
        labels = {
            "male": "🎙 Мужской",
            "female": "🎙 Женский",
            "energetic": "⚡ Энергичный",
        }
        return labels.get(voice_key, voice_key)