"""Transcriber — orchestrates faster-whisper on the extracted audio.

Stage D of the pipeline. Wraps the transcription service and returns
a flat list of segments with timestamps, ready for Stage E (LLM
clip selection).
"""
from dataclasses import dataclass
from pathlib import Path

from app.core.logging import get_logger
from app.services.transcription import (
    TranscriptionError,
    TranscriptionRequest,
    TranscriptionService,
)
from app.services.transcription.base import Segment, TranscriptionResult
from app.services.transcription.faster_whisper import get_transcription_service

logger = get_logger(__name__)


class TranscriberError(RuntimeError):
    """Stage D failed."""


@dataclass(slots=True, frozen=True)
class TranscribedJob:
    """What Stage E needs to pick interesting moments."""
    language: str
    duration_seconds: float
    segments: tuple[Segment, ...]
    raw_text: str
    model: str


class Transcriber:
    """Thin orchestrator over the abstract TranscriptionService."""

    def __init__(self, service: TranscriptionService | None = None) -> None:
        self._service = service or get_transcription_service()

    async def transcribe(
        self,
        audio_path: Path,
        *,
        language: str = "ru",
        beam_size: int = 1,
        word_timestamps: bool = False,
    ) -> TranscribedJob:
        if not audio_path.exists():
            raise TranscriberError(f"Audio not found: {audio_path}")

        try:
            result: TranscriptionResult = await self._service.transcribe(
                TranscriptionRequest(
                    audio_path=str(audio_path),
                    language=language,
                    beam_size=beam_size,
                    word_timestamps=word_timestamps,
                )
            )
        except TranscriptionError as e:
            raise TranscriberError(str(e)) from e

        # Whisper sometimes returns zero segments for pure silence or
        # non-speech audio. That's not an error — Stage E will say so.
        logger.info(
            "transcribed",
            segments=len(result.segments),
            language=result.language,
            duration=round(result.duration_seconds, 1),
        )
        return TranscribedJob(
            language=result.language,
            duration_seconds=result.duration_seconds,
            segments=result.segments,
            raw_text=result.raw_text,
            model=result.model,
        )