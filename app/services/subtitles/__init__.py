"""Subtitle services.

Stage H of the pipeline. We build an ASS (Advanced SubStation Alpha)
subtitle file from the Whisper transcript, then burn it into the
vertical MP4 with ffmpeg's `ass` filter.

Phrase-level (not karaoke word-level) per the design decision — easier
to read on mobile, less CPU on rendering, and good enough.
"""
from app.services.subtitles.base import (
    Phrase,
    SubtitleBuildRequest,
    SubtitleBuildResult,
    SubtitleBuilder,
    TranscriptSegment,
)
from app.services.subtitles.ass import AssSubtitleBuilder

__all__ = [
    "Phrase",
    "SubtitleBuildRequest",
    "SubtitleBuildResult",
    "SubtitleBuilder",
    "TranscriptSegment",
    "AssSubtitleBuilder",
]