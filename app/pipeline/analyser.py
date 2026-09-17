"""Analyser — orchestrates Stage E (LLM clip selection).

Stage E consumes Stage D's `TranscribedJob` and returns the validated
list of `ClipCandidate`s. It does NOT do any of:
- Whisper timestamps → boundary adjustment (that's Stage F)
- LLM retry policies (that's inside the selector)
- persistence (that's the handler)
"""
from dataclasses import dataclass
from typing import Sequence

from app.core.config import get_settings
from app.core.logging import get_logger
from app.pipeline.transcriber import TranscribedJob
from app.services.analysis.base import (
    ClipCandidate,
    ClipSelector,
    ClipSelectorError,
    ClipSelection,
    ClipSelectionRequest,
    TranscriptSegment,
)
from app.services.analysis.openrouter import get_clip_selector

logger = get_logger(__name__)


class AnalyserError(RuntimeError):
    """Stage E failed."""


@dataclass(slots=True, frozen=True)
class AnalysedJob:
    """What Stage F needs to cut clips."""
    clips: tuple[ClipCandidate, ...]
    model: str
    language: str


class Analyser:
    """Thin orchestrator over the abstract ClipSelector."""

    def __init__(
        self,
        selector: ClipSelector | None = None,
        target_count: int | None = None,
        min_seconds: float | None = None,
        max_seconds: float | None = None,
    ) -> None:
        s = get_settings()
        self._selector = selector or get_clip_selector()
        self._target_count = target_count or s.default_clip_count
        self._min_seconds = min_seconds or s.clip_min_seconds
        self._max_seconds = max_seconds or s.clip_max_seconds

    async def analyse(self, transcribed: TranscribedJob) -> AnalysedJob:
        request = ClipSelectionRequest(
            transcript=transcribed.raw_text,
            segments=tuple(
                TranscriptSegment(start=s.start, end=s.end, text=s.text)
                for s in transcribed.segments
            ),
            total_duration_seconds=transcribed.duration_seconds,
            target_count=self._target_count,
            min_seconds=self._min_seconds,
            max_seconds=self._max_seconds,
        )
        try:
            result: ClipSelection = await self._selector.select_clips(request)
        except ClipSelectorError as e:
            raise AnalyserError(str(e)) from e

        # If model returned more than requested, slice down.
        clips = result.clips[: self._target_count]
        if not clips:
            raise AnalyserError("LLM returned zero valid clips")

        logger.info(
            "analysed",
            clips=len(clips),
            model=result.model,
            target_count=self._target_count,
        )
        return AnalysedJob(
            clips=clips,
            model=result.model,
            language=transcribed.language,
        )