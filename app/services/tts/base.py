"""TTS Provider abstraction."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass(slots=True, frozen=True)
class AudioResult:
    """Result of TTS synthesis."""
    audio_bytes: bytes
    content_type: str  # e.g., "audio/mpeg"
    extension: str     # e.g., ".mp3"
    duration_seconds: Optional[float] = None
    provider: str = ""
    model: str = ""

    @property
    def size_bytes(self) -> int:
        return len(self.audio_bytes)


class TTSProvider(ABC):
    """Abstract TTS provider interface."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider identifier (e.g., 'openrouter')."""
        ...

    @property
    @abstractmethod
    def supported_voices(self) -> dict[str, str]:
        """
        Map of internal voice keys to provider-specific voice IDs.
        Example: {"male": "onyx", "female": "nova", "energetic": "shimmer"}
        """
        ...

    @property
    @abstractmethod
    def default_voice(self) -> str:
        """Default voice key."""
        ...

    @abstractmethod
    async def synthesize(
        self,
        text: str,
        voice: str,
        style: Optional[str] = None,
    ) -> AudioResult:
        """
        Synthesize speech from text.

        Args:
            text: Text to synthesize
            voice: Voice key from supported_voices
            style: Optional style hint (e.g., "energetic", "calm")

        Returns:
            AudioResult with audio bytes and metadata

        Raises:
            TTSError: On synthesis failure
        """
        ...

    async def health_check(self) -> bool:
        """Check if provider is available."""
        return True


class TTSError(Exception):
    """TTS provider error."""

    def __init__(self, message: str, code: str = "TTS_ERROR", provider: str = "", retryable: bool = False):
        self.code = code
        self.provider = provider
        self.retryable = retryable
        super().__init__(message)