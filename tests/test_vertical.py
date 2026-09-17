"""Tests for Stage G (VerticalRenderer).

Real ffmpeg path; verifies:
- Landscape input produces vertical 1080x1920 output
- Portrait input (already vertical) still renders correctly
- The output has the right resolution + non-zero size
"""
import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest


def _ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


pytestmark = pytest.mark.skipif(
    not _ffmpeg_available(), reason="ffmpeg not installed on this host"
)


def _make_video(
    path: Path,
    duration: float = 3.0,
    width: int = 640,
    height: int = 480,
    fps: int = 30,
) -> None:
    """Create a test video with sine audio + solid color at given dimensions."""
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi",
        "-i", f"sine=frequency=1000:duration={duration}",
        "-f", "lavfi",
        "-i", f"color=c=blue:s={width}x{height}:d={duration}:r={fps}",
        "-shortest",
        "-c:v", "libx264",
        "-c:a", "aac",
        "-pix_fmt", "yuv420p",
        "-g", str(fps),
        str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"test fixture ffmpeg failed: {proc.stderr[:300]}")


@pytest.fixture()
def landscape_video(tmp_path: Path) -> Path:
    p = tmp_path / "input_640x480.mp4"
    _make_video(p, width=640, height=480)
    return p


@pytest.fixture()
def portrait_video(tmp_path: Path) -> Path:
    p = tmp_path / "input_360x640.mp4"
    _make_video(p, width=360, height=640)
    return p


def _probe(path: Path) -> dict:
    """Quick ffprobe via ffmpeg -i stderr parsing isn't reliable; use a tiny shim."""
    from app.services.media import get_probe_service
    return asyncio.run(get_probe_service().probe(path))


def test_make_vertical_landscape(landscape_video: Path, tmp_path: Path):
    from app.services.media import get_media_service

    out = tmp_path / "out.mp4"
    asyncio.run(
        get_media_service().make_vertical(
            landscape_video, out,
            target_width=540, target_height=960,
        )
    )
    meta = _probe(out)
    assert meta.width == 540
    assert meta.height == 960
    assert meta.has_audio


def test_make_vertical_portrait(portrait_video: Path, tmp_path: Path):
    """Portrait input should still produce correct output."""
    from app.services.media import get_media_service

    out = tmp_path / "out.mp4"
    asyncio.run(
        get_media_service().make_vertical(
            portrait_video, out,
            target_width=540, target_height=960,
        )
    )
    meta = _probe(out)
    assert meta.width == 540
    assert meta.height == 960


def test_renderer_produces_verticals(landscape_video: Path, tmp_path: Path):
    """Full pipeline: cut job -> vertical renderer."""
    from app.pipeline.clip_cutter import CutJob, CutClip
    from app.pipeline.vertical_renderer import VerticalRenderer
    from app.services.analysis.base import ClipCandidate

    cut_job = CutJob(
        clips=(
            CutClip(
                index=1,
                source_start=0.0, source_end=2.0,
                candidate=ClipCandidate(
                    start=0.0, end=2.0, title="A", hook="h", reason="r",
                ),
                output_path=landscape_video,
            ),
        ),
        model="fake",
    )
    out_dir = tmp_path / "out"
    result = asyncio.run(
        VerticalRenderer(target_width=540, target_height=960).render(cut_job, out_dir)
    )
    assert len(result.clips) == 1
    c = result.clips[0]
    assert c.output_path.exists()
    assert c.size_bytes > 0
    meta = _probe(c.output_path)
    assert meta.width == 540
    assert meta.height == 960


def test_renderer_missing_input(tmp_path: Path):
    from app.pipeline.clip_cutter import CutJob, CutClip
    from app.pipeline.vertical_renderer import VerticalRenderError, VerticalRenderer
    from app.services.analysis.base import ClipCandidate

    cut_job = CutJob(
        clips=(
            CutClip(
                index=1, source_start=0.0, source_end=2.0,
                candidate=ClipCandidate(
                    start=0.0, end=2.0, title="A", hook="h", reason="r",
                ),
                output_path=tmp_path / "nope.mp4",
            ),
        ),
        model="fake",
    )
    with pytest.raises(VerticalRenderError):
        asyncio.run(VerticalRenderer().render(cut_job, tmp_path))


def test_make_vertical_missing_input(tmp_path: Path):
    from app.services.media import get_media_service
    with pytest.raises(FileNotFoundError):
        asyncio.run(
            get_media_service().make_vertical(tmp_path / "nope.mp4", tmp_path / "out.mp4")
        )