"""Tests for OpenRouter token optimization.

Verifies:
- _fmt_time uses MM:SS not HH:MM:SS
- _build_user_payload truncates to MAX_TRANSCRIPT_CHARS at segment boundary
- _build_user_payload keeps LAST segments when truncated
- Short transcripts pass through unchanged
"""
import pytest

from app.services.analysis.openrouter import OpenRouterClipSelector, MAX_TRANSCRIPT_CHARS
from app.services.analysis.base import ClipSelectionRequest, TranscriptSegment


def test_fmt_time_uses_compact_mm_ss():
    assert OpenRouterClipSelector._fmt_time(0) == "00:00"
    assert OpenRouterClipSelector._fmt_time(5) == "00:05"
    assert OpenRouterClipSelector._fmt_time(60) == "01:00"
    assert OpenRouterClipSelector._fmt_time(125) == "02:05"
    assert OpenRouterClipSelector._fmt_time(3600) == "60:00"  # 1 hour
    # Compare savings vs HH:MM:SS
    assert len(OpenRouterClipSelector._fmt_time(125)) < len("00:02:05")


def _build_request(segments, total: float = 600.0) -> ClipSelectionRequest:
    return ClipSelectionRequest(
        transcript=" ".join(s.text for s in segments),
        segments=tuple(segments),
        total_duration_seconds=total,
        target_count=3,
        min_seconds=20.0,
        max_seconds=60.0,
    )


def test_short_transcript_passes_through_unchanged():
    """A short transcript (< MAX_TRANSCRIPT_CHARS) is sent whole."""
    sel = OpenRouterClipSelector(api_key="k")
    req = _build_request([
        TranscriptSegment(start=0, end=10, text="привет"),
        TranscriptSegment(start=10, end=20, text="мир"),
    ])
    payload = sel._build_user_payload(req)
    # The full transcript block is included
    assert "привет" in payload
    assert "мир" in payload
    # No truncation marker
    assert "[truncated" not in payload


def test_long_transcript_truncated_at_segment_boundary():
    """A long transcript is truncated to MAX_TRANSCRIPT_CHARS, keeping
    the LAST segments, with a '[truncated, last segments only]' marker.
    """
    sel = OpenRouterClipSelector(api_key="k")
    # Build a 50-segment transcript, each ~100 chars → ~5500 chars total
    segs = [
        TranscriptSegment(
            start=i * 10, end=(i + 1) * 10,
            text=f"сегмент {i:03d} " + "x" * 80,  # ~95 chars
        )
        for i in range(50)
    ]
    req = _build_request(segs)
    payload = sel._build_user_payload(req)
    # Truncation marker is present
    assert "[truncated" in payload
    # The first few segments are GONE; the last ones are present
    assert "сегмент 000" not in payload
    assert "сегмент 049" in payload
    # Length is bounded near MAX_TRANSCRIPT_CHARS (header + marker overhead)
    assert len(payload) < MAX_TRANSCRIPT_CHARS + 200


def test_truncation_aligns_to_segment_start():
    """The truncated payload starts with a complete segment line, not
    a half-cut one. We verify by checking that every line starts with
    '[MM:SS-' which only happens at segment boundaries.
    """
    sel = OpenRouterClipSelector(api_key="k")
    segs = [
        TranscriptSegment(
            start=i * 10, end=(i + 1) * 10,
            text=f"текст {i:03d} " + "y" * 100,
        )
        for i in range(60)
    ]
    req = _build_request(segs)
    payload = sel._build_user_payload(req)
    # Each line after the header should start with '['
    body_lines = payload.split("\n")[1:]  # drop header line
    for line in body_lines:
        if not line:  # skip blank
            continue
        if line.startswith("["):  # truncation marker
            continue
        # Otherwise should start with timestamp '['
        assert line.startswith("["), f"misaligned line: {line[:60]!r}"


def test_payload_includes_required_metadata():
    sel = OpenRouterClipSelector(api_key="k")
    req = _build_request([TranscriptSegment(start=0, end=5, text="тест")])
    payload = sel._build_user_payload(req)
    # Required header fields
    assert "DUR:" in payload
    assert "COUNT:3" in payload
    assert "LEN:20-60" in payload


def test_transcript_with_negative_timestamps_clamps_to_zero():
    sel = OpenRouterClipSelector(api_key="k")
    segs = [
        TranscriptSegment(start=-5, end=5, text="выровнено"),
    ]
    req = _build_request(segs)
    payload = sel._build_user_payload(req)
    # _fmt_time clamps negative to 0
    assert "[00:00-00:05]" in payload