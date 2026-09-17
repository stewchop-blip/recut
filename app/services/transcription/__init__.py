"""Transcription services.

`TranscriptionService` is the abstract interface so we can swap
faster-whisper for an external API later (the requirement explicitly
calls this out — STT must be replaceable).
"""
from app.services.transcription.base import (
    Segment,
    TranscriptionError,
    TranscriptionRequest,
    TranscriptionResult,
    TranscriptionService,
    Word,
)

__all__ = [
    "Segment",
    "Word",
    "TranscriptionRequest",
    "TranscriptionResult",
    "TranscriptionService",
    "TranscriptionError",
]