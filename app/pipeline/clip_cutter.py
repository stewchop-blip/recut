"""ClipCutter — cuts source video into N MP4 clips.

Stage F of the pipeline. Takes the LLM-suggested moments from Stage E
and the Whisper segments from Stage D, and:

1. Adjusts each clip's start/end to the nearest Whisper word boundary
   so we never cut mid-word or mid-sentence.
2. Applies configured start/end padding.
3. Runs `ffmpeg -c copy` to produce lossless MP4 cuts.
4. Verifies output file is non-empty.

After this stage each clip is a raw MP4 in the source's original
resolution and aspect ratio. Vertical conversion happens in Stage G.
"""
from dataclasses import dataclass
from pathlib import Path

from app.core.config import get_settings
from app.core.logging import get_logger
from app.pipeline.analyser import AnalysedJob
from app.pipeline.transcriber import TranscribedJob
from app.services.analysis.base import ClipCandidate
from app.services.media import get_media_service
from app.services.transcription.base import Segment

logger = get_logger(__name__)


class ClipCutterError(RuntimeError):
    """Stage F failed."""


@dataclass(slots=True, frozen=True)
class CutClip:
    """One cut clip, ready for downstream rendering."""
    index: int                     # 1-based, for naming "clip_01.mp4"
    source_start: float            # what we asked ffmpeg for (after boundary adjust)
    source_end: float
    candidate: ClipCandidate       # original LLM suggestion
    output_path: Path


@dataclass(slots=True, frozen=True)
class CutJob:
    """Result of Stage F — N cut clips on disk."""
    clips: tuple[CutClip, ...]
    model: str


class ClipCutter:
    """Stateless service."""

    def __init__(
        self,
        start_padding_seconds: float | None = None,
        end_padding_seconds: float | None = None,
        min_duration_seconds: float = 5.0,
        max_duration_seconds: float = 180.0,
    ) -> None:
        s = get_settings()
        self._start_pad = start_padding_seconds or s.clip_start_padding_seconds
        self._end_pad = end_padding_seconds or s.clip_end_padding_seconds
        self._min_dur = min_duration_seconds
        self._max_dur = max_duration_seconds

    # ---------------------------------------------------------------------------
    # Public
    # ---------------------------------------------------------------------------

    async def cut(
        self,
        analysed: AnalysedJob,
        transcribed: TranscribedJob,
        source_video: Path,
        output_dir: Path,
    ) -> CutJob:
        """Cut each LLM-suggested moment to its own MP4.

        `output_dir` is the job_dir; we write `clip_NN.mp4` there.
        """
        if not source_video.exists():
            raise ClipCutterError(f"Source video not found: {source_video}")
        output_dir.mkdir(parents=True, exist_ok=True)

        media = get_media_service()
        segments = transcribed.segments

        out_clips: list[CutClip] = []
        for i, candidate in enumerate(analysed.clips, start=1):
            try:
                start, end = self._adjust_boundaries(
                    candidate, segments, transcribed.duration_seconds,
                )
            except ClipCutterError as e:
                logger.warning(
                    "clip_boundary_adjust_failed",
                    index=i, candidate_start=candidate.start, candidate_end=candidate.end,
                    error=str(e),
                )
                continue

            out_path = output_dir / f"clip_{i:02d}.mp4"
            try:
                await media.cut_clip(
                    input_path=source_video,
                    output_path=out_path,
                    start_seconds=start,
                    end_seconds=end,
                )
            except (RuntimeError, ValueError) as e:
                logger.error(
                    "clip_cut_failed", index=i, start=start, end=end, error=str(e)[:200],
                )
                continue

            out_clips.append(CutClip(
                index=i,
                source_start=start,
                source_end=end,
                candidate=candidate,
                output_path=out_path,
            ))

        if not out_clips:
            raise ClipCutterError("No clips could be cut")

        logger.info(
            "clips_cut",
            count=len(out_clips),
            model=analysed.model,
            dir=str(output_dir),
        )
        return CutJob(clips=tuple(out_clips), model=analysed.model)

    # ---------------------------------------------------------------------------
    # Boundary adjustment
    # ---------------------------------------------------------------------------

    def _adjust_boundaries(
        self,
        candidate: ClipCandidate,
        segments: tuple[Segment, ...],
        total_duration: float,
    ) -> tuple[float, float]:
        """Return (start, end) snapped to nearest Whisper segment boundaries.

        Rules:
        - Never cut in the middle of a word: snap start to the segment
          whose end >= candidate.start; same for end.
        - Add configured padding (clamped to [0, total]).
        - If the result is too short (< 5 s) or end < start, reject.
        """
        if not segments:
            raise ClipCutterError("No segments for boundary adjustment")

        start = max(0.0, candidate.start - self._start_pad)
        end = min(total_duration, candidate.end + self._end_pad)

        # Snap start to nearest segment boundary.
        snapped_start = _snap_to_segment_boundary(start, segments, prefer="before")
        # Snap end to nearest segment boundary (prefer "before" so we don't overshoot).
        snapped_end = _snap_to_segment_boundary(end, segments, prefer="before")

        if snapped_end <= snapped_start:
            raise ClipCutterError(
                f"After snapping, end ({snapped_end}) <= start ({snapped_start})"
            )
        if snapped_end - snapped_start < self._min_dur:
            raise ClipCutterError(
                f"Clip too short after snap: {snapped_end - snapped_start:.1f}s"
            )
        if snapped_end - snapped_start > self._max_dur:
            # Hard ceiling — protects against a runaway LLM suggestion.
            snapped_end = snapped_start + self._max_dur

        return round(snapped_start, 3), round(snapped_end, 3)


def _snap_to_segment_boundary(
    target: float,
    segments: tuple[Segment, ...],
    *,
    prefer: str = "before",
) -> float:
    """Snap `target` to the nearest segment start.

    prefer="before" — return the segment whose start is just <= target
    (never overshoot). prefer="after" — return the segment whose start is
    just >= target (no earlier content).
    """
    # Binary-search-like: find the latest segment whose start <= target.
    best_before = segments[0].start
    for seg in segments:
        if seg.start <= target:
            best_before = seg.start
        else:
            break

    if prefer == "before":
        return best_before
    # prefer == "after"
    for seg in segments:
        if seg.start >= target:
            return seg.start
    return segments[-1].start