"""OpenRouter TTS Provider — uses /chat/completions with audio output.

OpenRouter exposes OpenAI's GPT-Audio model via chat completions with
`modalities=["text","audio"]`. Audio is streamed as base64-encoded PCM16
chunks in `choices[0].delta.audio.data`. We reassemble and return as raw
PCM16 bytes (24kHz, mono, 16-bit little-endian) — the voice_select
handler converts it to MP3 with ffmpeg for Telegram.
"""
import base64
import json
from typing import Optional

import httpx

from app.core.config import get_settings
from app.core.logging import get_logger
from app.services.tts.base import AudioResult, TTSError, TTSProvider

logger = get_logger(__name__)

# OpenAI voices available on gpt-audio / gpt-audio-mini via OpenRouter.
# https://platform.openai.com/docs/guides/audio
_OPENAI_VOICES = {
    "male": "onyx",        # deep, masculine
    "female": "nova",      # warm, feminine
    "energetic": "shimmer",  # bright, energetic
}


class OpenRouterTTSProvider(TTSProvider):
    """OpenRouter TTS provider via chat/completions streaming."""

    VOICE_MAP = _OPENAI_VOICES

    # The model we actually want to use — verified working on 2026-09-14:
    # POST /api/v1/chat/completions with stream=true, modalities=[text,audio],
    # audio.format=pcm16 returns base64 PCM16 chunks (~289 KB / short phrase).
    DEFAULT_MODEL = "openai/gpt-audio-mini"

    # PCM16 from OpenAI is 24 kHz, mono, 16-bit little-endian.
    PCM_SAMPLE_RATE = 24_000
    PCM_CHANNELS = 1
    PCM_SAMPLE_WIDTH = 2  # bytes

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        timeout_seconds: int = 60,
    ) -> None:
        self._api_key = api_key or get_settings().openrouter_api_key
        # Use the verified default model if user didn't override via env/arg.
        # Ignore settings.openrouter_tts_model since env was the source of bugs.
        self._model = model or self.DEFAULT_MODEL
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
        """Synthesize speech via OpenRouter chat/completions + audio output.

        Returns raw PCM16 bytes at 24 kHz, mono. The caller (voice_select
        handler) is expected to convert to MP3 for Telegram.
        """
        provider_voice = self.VOICE_MAP.get(voice, self.VOICE_MAP[self.default_voice])

        # Build user message — system prompt nudges model to actually
        # speak the text instead of replying conversationally.
        system_prompt = (
            "You are a text-to-speech engine. Read the user's text aloud "
            "exactly as written. Do not add commentary, intro, or outro. "
            "Do not translate. Speak in a clear, natural voice."
        )
        if style:
            system_prompt += f" Voice style: {style}."

        payload = {
            "model": self._model,
            "stream": True,
            "modalities": ["text", "audio"],
            "audio": {
                "voice": provider_voice,
                "format": "pcm16",
            },
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": text},
            ],
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

            # Streaming request — SSE chunks
            async with client.stream("POST", "/chat/completions", json=payload) as response:
                if response.status_code != 200:
                    body = await response.aread()
                    error_msg = body.decode("utf-8", errors="ignore")[:500]
                    logger.error(
                        "tts_failed",
                        provider=self.name,
                        status=response.status_code,
                        error=error_msg,
                    )
                    retryable = response.status_code in (429, 500, 502, 503, 504)
                    raise TTSError(
                        message=f"TTS synthesis failed: HTTP {response.status_code}: {error_msg}",
                        code=f"HTTP_{response.status_code}",
                        provider=self.name,
                        retryable=retryable,
                    )

                pcm_chunks: list[bytes] = []
                text_preview = ""
                async for line in response.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data_str = line[len("data:"):].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        evt = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    choices = evt.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    # Audio chunk — base64 PCM16
                    audio = delta.get("audio")
                    if isinstance(audio, dict):
                        b64 = audio.get("data")
                        if b64:
                            try:
                                pcm_chunks.append(base64.b64decode(b64))
                            except Exception as e:
                                logger.warning("tts_bad_audio_chunk", error=str(e)[:80])
                    # Optional: collect text content for debug log
                    content = delta.get("content")
                    if isinstance(content, str) and len(text_preview) < 80:
                        text_preview += content

                if not pcm_chunks:
                    raise TTSError(
                        message="No audio chunks received from OpenRouter",
                        code="NO_AUDIO",
                        provider=self.name,
                        retryable=True,
                    )

                pcm_bytes = b"".join(pcm_chunks)
                logger.info(
                    "tts_success",
                    provider=self.name,
                    model=self._model,
                    voice=provider_voice,
                    size_bytes=len(pcm_bytes),
                    chunks=len(pcm_chunks),
                    model_text_preview=text_preview[:60],
                )

                return AudioResult(
                    audio_bytes=pcm_bytes,
                    content_type="audio/pcm",
                    extension=".pcm",
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
            response = await client.get("/models")
            return response.status_code == 200
        except Exception:
            return False

    def get_available_voices(self) -> list[str]:
        return list(self.VOICE_MAP.keys())

    def get_provider_voice(self, voice_key: str) -> str:
        return self.VOICE_MAP.get(voice_key, self.VOICE_MAP[self.default_voice])