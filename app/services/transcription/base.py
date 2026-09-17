"""Abstract transcription interface.

We deliberately keep the surface tiny: take a WAV path, give back
segments. No streaming, no callbacks — easier to mock and replace.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass(slots=True, frozen=True)
class Segment:
    """One speech segment from STT."""
    start: float
    end: float
    text: str
    # Optional word-level timestamps. Not populated by faster-whisper
    # unless word_timestamps=True — we keep it optional.
    words: tuple["Word", ...] = field(default_factory=tuple)


@dataclass(slots=True, frozen=True)
class Word:
    """One word with timestamps (optional, populated when available)."""
    start: float
    end: float
    text: str


@dataclass(slots=True, frozen=True)
class TranscriptionRequest:
    """Inputs to a transcription run."""
    audio_path: str
    language: str = "ru"          # ISO 639-1; "" = auto-detect
    beam_size: int = 1            # 1 is fastest on CPU
    word_timestamps: bool = False


@dataclass(slots=True, frozen=True)
class TranscriptionResult:
    """Result of a successful transcription."""
    segments: tuple[Segment, ...]
    language: str                 # detected or requested
    duration_seconds: float
    model: str
    raw_text: str                 # joined text for downstream consumers


class TranscriptionError(RuntimeError):
    """Base class for transcription failures."""


class TranscriptionService(ABC):
    """Abstract STT provider."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider identifier ('faster-whisper', 'openai-whisper-api', etc.)."""
        ...

    @property
    @abstractmethod
    def model_id(self) -> str:
        ...

    @abstractmethod
    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult:
        """Transcribe the audio file and return structured segments.

        Implementations must be safe to call concurrently? — no. faster-whisper
        uses an internal lock; callers should serialize if needed.
        """