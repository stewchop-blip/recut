"""Tests for Stage F (ClipCutter).

We use a real tiny video (lavfi sine + color) and check that ffmpeg
-c copy produces valid output MP4s at the requested boundaries.
"""
import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import pytest


def _ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


pytestmark = pytest.mark.skipif(
    not _ffmpeg_available(), reason="ffmpeg not installed on this host"
)


def _make_test_video(path: Path, duration: float = 5.0) -> None:
    """Create a short test video with a 1 kHz sine track and blue frames."""
    cmd = [
        "ffmpeg",
        "-y",
        "-f", "lavfi",
        "-i", f"sine=frequency=1000:duration={duration}",
        "-f", "lavfi",
        "-i", f"color=c=blue:s=640x480:d={duration}",
        "-shortest",
        "-c:v", "libx264",
        "-c:a", "aac",
        "-pix_fmt", "yuv420p",
        "-g", "30",                       # keyframe every 1s at 30fps
        str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"test fixture ffmpeg failed: {proc.stderr[:300]}")


@pytest.fixture()
def tiny_video(tmp_path: Path) -> Path:
    p = tmp_path / "input.mp4"
    _make_test_video(p, duration=5.0)
    return p


def _build_segments() -> tuple:
    """Whisper-like segments that align to whole seconds."""
    from app.services.transcription.base import Segment
    return (
        Segment(start=0.0, end=1.0, text="один"),
        Segment(start=1.0, end=2.0, text="два"),
        Segment(start=2.0, end=3.0, text="три"),
        Segment(start=3.0, end=4.0, text="четыре"),
        Segment(start=4.0, end=5.0, text="пять"),
    )


def _build_analysed_job(clips_spec: list[tuple[float, float, str]]) -> object:
    from app.pipeline.analyser import AnalysedJob
    from app.services.analysis.base import ClipCandidate
    return AnalysedJob(
        clips=tuple(
            ClipCandidate(start=s, end=e, title=t, hook="h", reason="r")
            for s, e, t in clips_spec
        ),
        model="fake",
        language="ru",
    )


def test_cutter_produces_mp4_files(tmp_path: Path, tiny_video: Path):
    from app.pipeline.clip_cutter import ClipCutter
    from app.pipeline.transcriber import TranscribedJob

    transcribed = TranscribedJob(
        language="ru",
        duration_seconds=5.0,
        segments=_build_segments(),
        raw_text="x",
        model="fake",
    )
    analysed = _build_analysed_job([
        (0.2, 1.8, "фрагмент 1"),
        (2.2, 3.8, "фрагмент 2"),
    ])

    job_dir = tmp_path / "job"
    cutter = ClipCutter(start_padding_seconds=0.0, end_padding_seconds=0.0, min_duration_seconds=1.0)
    result = asyncio.run(cutter.cut(analysed, transcribed, tiny_video, job_dir))

    assert len(result.clips) == 2
    for c in result.clips:
        assert c.output_path.exists()
        assert c.output_path.stat().st_size > 0
        assert c.output_path.suffix == ".mp4"


def test_cutter_snaps_to_segment_boundary(tmp_path: Path, tiny_video: Path):
    """A clip start at 1.7s must snap to 1.0 (nearest segment start before)."""
    from app.pipeline.clip_cutter import ClipCutter
    from app.pipeline.transcriber import TranscribedJob

    transcribed = TranscribedJob(
        language="ru",
        duration_seconds=5.0,
        segments=_build_segments(),
        raw_text="x",
        model="fake",
    )
    # candidate at 1.7s should snap to 1.0s
    analysed = _build_analysed_job([(1.7, 2.4, "snap test")])
    cutter = ClipCutter(start_padding_seconds=0.0, end_padding_seconds=0.0, min_duration_seconds=1.0)
    result = asyncio.run(cutter.cut(analysed, transcribed, tiny_video, tmp_path))
    assert len(result.clips) == 1
    assert result.clips[0].source_start == 1.0


def test_cutter_applies_padding(tmp_path: Path, tiny_video: Path):
    from app.pipeline.clip_cutter import ClipCutter
    from app.pipeline.transcriber import TranscribedJob

    transcribed = TranscribedJob(
        language="ru",
        duration_seconds=5.0,
        segments=_build_segments(),
        raw_text="x",
        model="fake",
    )
    # candidate at 1.0, with 0.5s padding on each side -> 0.5 to 3.5
    # snapped to nearest segment before: start 0.0, end 3.0
    analysed = _build_analysed_job([(1.0, 3.0, "padded")])
    cutter = ClipCutter(start_padding_seconds=0.5, end_padding_seconds=0.5, min_duration_seconds=1.0)
    result = asyncio.run(cutter.cut(analysed, transcribed, tiny_video, tmp_path))
    assert len(result.clips) == 1
    # snapped to nearest segment start <= target
    assert result.clips[0].source_start == 0.0
    assert result.clips[0].source_end == 3.0


def test_cutter_rejects_too_short_after_snap(tmp_path: Path, tiny_video: Path):
    """A candidate inside a single 1-sec segment yields <5s and is rejected."""
    from app.pipeline.clip_cutter import ClipCutter, ClipCutterError
    from app.pipeline.transcriber import TranscribedJob

    transcribed = TranscribedJob(
        language="ru",
        duration_seconds=5.0,
        segments=_build_segments(),
        raw_text="x",
        model="fake",
    )
    # candidate from 0.2 to 0.8 -> snaps to 0.0..0.0 -> <5s -> rejected
    analysed = _build_analysed_job([(0.2, 0.8, "tiny")])
    cutter = ClipCutter(min_duration_seconds=5.0)  # default — must reject 0s clip
    with pytest.raises(ClipCutterError):
        asyncio.run(cutter.cut(analysed, transcribed, tiny_video, tmp_path))


def test_cutter_handles_missing_source(tmp_path: Path):
    from app.pipeline.clip_cutter import ClipCutter, ClipCutterError
    from app.pipeline.transcriber import TranscribedJob

    transcribed = TranscribedJob(
        language="ru",
        duration_seconds=5.0,
        segments=_build_segments(),
        raw_text="x",
        model="fake",
    )
    analysed = _build_analysed_job([(0.0, 2.0, "x")])
    with pytest.raises(ClipCutterError):
        asyncio.run(
            ClipCutter().cut(analysed, transcribed, tmp_path / "nope.mp4", tmp_path)
        )


def test_snap_helper_prefer_before():
    from app.pipeline.clip_cutter import _snap_to_segment_boundary
    segs = _build_segments()
    # target 2.4 -> nearest seg.start <= 2.4 is 2.0
    assert _snap_to_segment_boundary(2.4, segs, prefer="before") == 2.0
    # target 0.0 -> 0.0
    assert _snap_to_segment_boundary(0.0, segs, prefer="before") == 0.0
    # target 6.0 (past end) -> 4.0 (last segment start)
    assert _snap_to_segment_boundary(6.0, segs, prefer="before") == 4.0