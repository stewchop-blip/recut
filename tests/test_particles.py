"""Real renders: the shared finish stays faint, animated and compatible."""
import json
import subprocess

import numpy as np
import pytest

from app.services.media.ffmpeg import MediaService, _drawtext
from app.services.overlays.particles import twinkle_filters


def run(*args):
    return subprocess.run(args, check=True, capture_output=True).stdout


def frames(path, width, height):
    raw = run("ffmpeg", "-v", "error", "-i", str(path), "-an", "-f", "rawvideo",
              "-pix_fmt", "gray", "-")
    return np.frombuffer(raw, dtype=np.uint8).reshape(-1, height, width)


def test_particles_are_sparse_faint_and_animated(tmp_path):
    # Lossless frames isolate the effect from compression and other styling.
    output = tmp_path / "particles.mkv"
    chain = twinkle_filters(0, 0, 540, 960, _drawtext)
    run("ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=black:s=540x960:r=4:d=3",
        "-vf", chain, "-c:v", "ffv1", str(output))
    pixels = frames(output, 540, 960)
    assert pixels.max() > 0, "Effect must actually reach the pixels"
    assert pixels.max() <= 20, "Peak must remain faint"
    assert np.count_nonzero(pixels) / pixels.size < 0.002, "Not a full-frame flicker"
    assert np.any(pixels[0] != pixels[-1]), "Opacity must change over time"


@pytest.mark.parametrize("layout,color,bg,title,brand,audio", [
    ("full", "original", "blur", "", False, False),
    ("pip", "punchy", "dark", "Тест", False, True),
    ("framed", "contrast", "accent", "", True, True),
])
async def test_shared_render_preserves_video_audio(tmp_path, layout, color, bg, title, brand, audio):
    source, output = tmp_path / "source.mp4", tmp_path / "out.mp4"
    args = ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=gray:s=180x320:r=12:d=1"]
    if audio:
        args += ["-f", "lavfi", "-i", "sine=frequency=440:duration=1"]
    run(*args, "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source))
    await MediaService().make_vertical(source, output, target_width=360, target_height=640,
        target_fps=12, layout_id=layout, color_preset=color, background_id=bg,
        title_text=title, brand_corner=brand)
    data = json.loads(run("ffprobe", "-v", "error", "-show_streams", "-show_format",
                          "-of", "json", str(output)))
    video = next(s for s in data["streams"] if s["codec_type"] == "video")
    assert (video["width"], video["height"]) == (360, 640)
    assert video["sample_aspect_ratio"] == "1:1"
    assert abs(float(data["format"]["duration"]) - 1.0) < 0.2
    assert any(s["codec_type"] == "audio" for s in data["streams"]) == audio
    if not audio:
        baseline = tmp_path / "baseline.mp4"
        await MediaService().make_vertical(source, baseline, target_width=360, target_height=640,
            target_fps=12, layout_id=layout, color_preset=color, background_id=bg,
            subtle_particles=False)
        changed = frames(output, 360, 640).astype(np.int16)
        original = frames(baseline, 360, 640).astype(np.int16)
        difference = abs(changed - original)
        assert difference.max() > 0, "Twinkles must survive actual MP4 encoding"
        assert difference.mean() < 0.1, "Visual finish must not alter overall exposure"
    if audio:
        # Verify the actual tone remains, not just an empty audio stream.
        raw = run("ffmpeg", "-v", "error", "-i", str(output), "-vn", "-ac", "1",
                  "-ar", "8000", "-f", "f32le", "-")
        samples = np.frombuffer(raw, dtype=np.float32)
        freq = np.fft.rfftfreq(len(samples), 1 / 8000)[np.argmax(abs(np.fft.rfft(samples)))]
        assert abs(freq - 440) < 5
