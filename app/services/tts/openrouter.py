"""OpenRouter TTS Provider implementation."""

import httpx
from typing import Optional

from app.core.config import get_settings
from app.core.logging import get_logger
from app.services.tts.base import AudioResult, TTSError, TTSProvider


logger = get_logger(__name__)


class OpenRouterTTSProvider(TTSProvider):
    """OpenRouter TTS provider using /api/v1/audio/speech endpoint."""

    # OpenAI-compatible voices (mapped to internal keys)
    VOICE_MAP = {
        "male": "onyx",       # deep, masculine
        "female": "nova",     # warm, feminine
        "energetic": "shimmer",  # bright, energetic
    }

    # Supported response formats
    SUPPORTED_FORMATS = {"mp3", "opus", "aac", "flac", "wav", "pcm"}

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        timeout_seconds: int = 30,
    ) -> None:
        self._api_key = api_key or get_settings().openrouter_api_key
        # Default to verified Gemini TTS if no override is provided
        self._model = model or get_settings().openrouter_tts_model
        self._timeout = timeout_seconds
        self._client: httpx.AsyncClient | None = None
        logger.info("tts_provider_init", model=self._model)

    @property
    def name(self) -> str:
        return "openrouter"

    @property
    def supported_voices(self) -> dict[str, str]:
        return self.VOICE_MAP.copy()

    @property
    def default_voice(self) -> str:
        return "male"

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url="https://openrouter.ai/api/v1",
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://recut.bot",
                    "X-Title": "Recut Bot",
                },
                timeout=httpx.Timeout(self._timeout, connect=10.0),
            )
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def synthesize(
        self,
        text: str,
        voice: str,
        style: Optional[str] = None,
    ) -> AudioResult:
        """Synthesize speech via OpenRouter audio/speech endpoint."""
        # Resolve voice
        if "gemini" in self._model:
            provider_voice = {"male": "nova", "female": "nova", "energetic": "nova"}.get(voice, "nova")
        else:
            provider_voice = self.VOICE_MAP.get(voice, self.VOICE_MAP[self.default_voice])

        # Build request
        payload = {
            "model": self._model,
            "input": text,
            "voice": provider_voice,
            "response_format": "pcm" if "gemini" in self._model else "mp3",
        }

        # Add style instructions via provider options if provided
        if style:
            payload["provider"] = {
                "options": {
                    "openai": {
                        "instructions": style,
                    }
                }
            }

        client = await self._get_client()

        try:
            logger.debug(
                "tts_request",
                provider=self.name,
                model=self._model,
                voice=provider_voice,
                text_length=len(text),
            )

            response = await client.post(
                "/audio/speech",
                json=payload,
            )

            # Handle errors
            if response.status_code != 200:
                error_data = {}
                try:
                    error_data = response.json()
                except Exception:
                    error_data = {"raw": response.text[:500]}

                error_msg = error_data.get("error", {}).get("message", f"HTTP {response.status_code}")
                logger.error(
                    "tts_failed",
                    provider=self.name,
                    status=response.status_code,
                    error=error_msg,
                )

                # Determine if retryable
                retryable = response.status_code in (429, 500, 502, 503, 504)
                raise TTSError(
                    message=f"TTS synthesis failed: {error_msg}",
                    code=f"HTTP_{response.status_code}",
                    provider=self.name,
                    retryable=retryable,
                )

            # Get audio bytes
            audio_bytes = response.content
            if not audio_bytes:
                raise TTSError(
                    message="Empty audio response",
                    code="EMPTY_RESPONSE",
                    provider=self.name,
                    retryable=True,
                )

            # Get generation ID from headers if available
            generation_id = response.headers.get("X-Generation-Id", "")

            logger.info(
                "tts_success",
                provider=self.name,
                model=self._model,
                voice=provider_voice,
                size_bytes=len(audio_bytes),
                generation_id=generation_id,
            )

            response_format = payload["response_format"]
            return AudioResult(
                audio_bytes=audio_bytes,
                content_type="audio/wav" if response_format == "wav" else f"audio/{response_format}",
                extension=f".{response_format}",
                provider=self.name,
                model=self._model,
            )

        except httpx.TimeoutException as e:
            logger.error("tts_timeout", provider=self.name, error=str(e))
            raise TTSError(
                message="TTS request timed out",
                code="TIMEOUT",
                provider=self.name,
                retryable=True,
            ) from e

        except httpx.RequestError as e:
            logger.error("tts_request_error", provider=self.name, error=str(e))
            raise TTSError(
                message=f"TTS request failed: {e}",
                code="REQUEST_ERROR",
                provider=self.name,
                retryable=True,
            ) from e

    async def health_check(self) -> bool:
        """Check if OpenRouter API is reachable."""
        try:
            client = await self._get_client()
            response = await client.get("/models", params={"output_modalities": "speech"})
            return response.status_code == 200
        except Exception:
            return False

    def get_available_voices(self) -> list[str]:
        """Get list of available voice keys."""
        return list(self.VOICE_MAP.keys())

    def get_provider_voice(self, voice_key: str) -> str:
        """Get provider-specific voice ID."""
        return self.VOICE_MAP.get(voice_key, self.VOICE_MAP[self.default_voice])