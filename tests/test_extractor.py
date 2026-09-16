"""Tests for the audio extractor (Stage C).

These tests use real ffmpeg via the `app.services.media` wrapper — they
generate a tiny test video with `ffmpeg` (lavfi) and verify that we can
extract its audio track. If ffmpeg isn't on PATH the tests skip
cleanly so CI environments without it don't fail spuriously.
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


def _make_test_video(path: Path, duration: float = 1.0) -> None:
    """Create a 1-second test video with sine-wave audio via lavfi."""
    cmd = [
        "ffmpeg",
        "-y",
        "-f", "lavfi",
        "-i", f"sine=frequency=440:duration={duration}",
        "-f", "lavfi",
        "-i", f"color=c=blue:s=320x240:d={duration}",
        "-shortest",
        "-c:v", "libx264",
        "-c:a", "aac",
        "-pix_fmt", "yuv420p",
        str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"test fixture ffmpeg failed: {proc.stderr[:300]}")


@pytest.fixture()
def tiny_video(tmp_path: Path) -> Path:
    p = tmp_path / "input.mp4"
    _make_test_video(p, duration=1.0)
    return p


def test_extract_audio_produces_wav(tmp_path: Path, tiny_video: Path):
    from app.pipeline.extractor import AudioExtractor

    out = tmp_path / "audio.wav"
    extracted = asyncio.run(
        AudioExtractor().extract(tiny_video, out, has_audio=True)
    )
    assert extracted.path == out
    assert out.exists()
    assert out.stat().st_size > 0
    # 1 second of 16 kHz mono 16-bit = 32000 bytes
    assert extracted.sample_rate == 16_000
    assert extracted.channels == 1
    assert 0.5 < extracted.duration_seconds < 1.5


def test_extract_audio_rejects_video_without_audio(tmp_path: Path):
    """If we pass has_audio=False the extractor must raise."""
    from app.pipeline.extractor import AudioExtractor, AudioExtractionError

    fake_input = tmp_path / "input.mp4"
    fake_input.write_bytes(b"")  # empty file is fine — we short-circuit
    out = tmp_path / "audio.wav"

    with pytest.raises(AudioExtractionError):
        asyncio.run(AudioExtractor().extract(fake_input, out, has_audio=False))


def test_extract_audio_missing_input_raises(tmp_path: Path):
    """Real input path that doesn't exist must surface a clean error."""
    from app.pipeline.extractor import AudioExtractor, AudioExtractionError

    missing = tmp_path / "nope.mp4"
    out = tmp_path / "audio.wav"

    with pytest.raises(AudioExtractionError):
        asyncio.run(AudioExtractor().extract(missing, out, has_audio=True))


def test_extract_audio_custom_sample_rate(tmp_path: Path, tiny_video: Path):
    """Passing sample_rate=8000 should still produce a valid WAV."""
    from app.pipeline.extractor import AudioExtractor

    out = tmp_path / "audio_8k.wav"
    extracted = asyncio.run(
        AudioExtractor(sample_rate=8_000, channels=1).extract(
            tiny_video, out, has_audio=True,
        )
    )
    assert extracted.sample_rate == 8_000
    assert out.exists() and out.stat().st_size > 0