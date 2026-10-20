"""Integration checks for safe no-reencode normalization paths."""
import json
import shutil
import subprocess

import pytest

from app.services.media.normalizer import SourceNormalizer


def _ffprobe(path):
    return json.loads(subprocess.check_output([
        shutil.which("ffprobe") or "ffprobe", "-v", "error",
        "-show_streams", "-of", "json", str(path),
    ], text=True))


def _video_stream_hash(path):
    output = subprocess.check_output([
        shutil.which("ffmpeg") or "ffmpeg", "-v", "error", "-i", str(path),
        "-map", "0:v:0", "-c:v", "copy", "-f", "streamhash",
        "-hash", "sha256", "-",
    ], text=True)
    line = next(line for line in output.splitlines() if "SHA256=" in line)
    return line.split("SHA256=", 1)[1]


@pytest.mark.asyncio
async def test_normalizer_remuxes_clean_h264_aac_without_reencoding(tmp_path, monkeypatch):
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("FFmpeg and ffprobe are required")

    source = tmp_path / "source.mp4"
    output = tmp_path / "normalized.mp4"
    subprocess.run([
        ffmpeg, "-y", "-v", "error", "-f", "lavfi", "-i",
        "testsrc2=size=360x640:rate=24:duration=2", "-f", "lavfi", "-i",
        "sine=frequency=440:duration=2", "-c:v", "libx264", "-preset",
        "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
        str(source),
    ], check=True, timeout=30)

    normalizer = SourceNormalizer()
    monkeypatch.setattr(normalizer, "_detect_bars", _no_bars)
    result = await normalizer.normalize(source, output)

    streams = _ffprobe(output)["streams"]
    video = next(stream for stream in streams if stream["codec_type"] == "video")
    audio = next(stream for stream in streams if stream["codec_type"] == "audio")
    assert (video["width"], video["height"]) == (360, 640)
    assert video["codec_name"] == "h264"
    assert audio["codec_name"] == "aac"
    assert _video_stream_hash(source) == _video_stream_hash(output)
    assert result.source == output
    assert not result.crop_applied


@pytest.mark.asyncio
async def test_normalizer_keeps_transcode_path_for_other_video_codecs(tmp_path):
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("FFmpeg and ffprobe are required")

    source = tmp_path / "source.mp4"
    output = tmp_path / "normalized.mp4"
    subprocess.run([
        ffmpeg, "-y", "-v", "error", "-f", "lavfi", "-i",
        "testsrc2=size=360x640:rate=24:duration=1", "-c:v", "mpeg4",
        str(source),
    ], check=True, timeout=30)

    await SourceNormalizer().normalize(source, output)
    video = next(stream for stream in _ffprobe(output)["streams"]
                 if stream["codec_type"] == "video")
    assert video["codec_name"] == "h264"


@pytest.mark.asyncio
async def test_normalizer_still_crops_stable_black_bars(tmp_path):
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("FFmpeg and ffprobe are required")

    source = tmp_path / "letterboxed.mp4"
    output = tmp_path / "normalized.mp4"
    subprocess.run([
        ffmpeg, "-y", "-v", "error", "-f", "lavfi", "-i",
        "testsrc2=size=360x360:rate=24:duration=2,pad=360:640:0:140:black",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source),
    ], check=True, timeout=30)

    result = await SourceNormalizer().normalize(source, output)
    video = next(stream for stream in _ffprobe(output)["streams"]
                 if stream["codec_type"] == "video")
    assert result.crop_applied
    assert video["height"] < 640


async def _no_bars(path):
    return None
