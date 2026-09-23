"""Integration acceptance test for CTA overlay visibility.

Proves the CTA banner is actually rendered where expected (not just that
`has_cta == True`). Generates a blue 720x1280 video, burns a bright red
400x100 PNG at the bottom during the last 2 seconds, then extracts frames
inside and outside the CTA window and counts red pixels.

Run manually (needs ffmpeg + Pillow):

    python -m pytest tests/test_cta_acceptance.py -s -v

This is NOT run in the default unit suite because it shells out to ffmpeg
and is slow; the audit (Phase 1.7) requires it as a manual integration check.
"""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest


def _ffmpeg() -> str:
    import shutil
    p = shutil.which("ffmpeg")
    assert p, "ffmpeg not found in PATH"
    return p


def _run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _count_red(path: Path, y0: int, y1: int) -> int:
    from PIL import Image
    im = Image.open(path).convert("RGB")
    w, h = im.size
    cnt = 0
    for y in range(y0, min(y1, h)):
        for x in range(w):
            r, g, b = im.getpixel((x, y))
            if r > 200 and g < 80 and b < 80:
                cnt += 1
    return cnt


@pytest.mark.parametrize("w,h", [(720, 1280), (1080, 1920), (540, 960)])
def test_cta_visible_at_bottom_last_2s(tmp_path: Path, w: int, h: int) -> None:
    from app.services.media.ffmpeg import MediaService

    src = tmp_path / "blue.mp4"
    cta = tmp_path / "cta_red.png"
    out = tmp_path / "out.mp4"

    # Blue source video, 3 seconds.
    _run([
        _ffmpeg(), "-y", "-v", "error",
        "-f", "lavfi", "-i", f"color=c=blue:s={w}x{h}:d=3:r=30",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src),
    ])

    # Bright red 400x100 CTA.
    from PIL import Image
    Image.new("RGBA", (400, 100), (255, 0, 0, 255)).save(cta)

    asyncio.run(MediaService().burn_cta(
        src, cta, out,
        position="bottom", margin=120,
        start_seconds=1.0, end_seconds=3.0,
    ))

    f_during = tmp_path / "during.png"
    f_before = tmp_path / "before.png"
    _run([_ffmpeg(), "-y", "-v", "error", "-ss", "2.0", "-i", str(out),
          "-frames:v", "1", str(f_during)])
    _run([_ffmpeg(), "-y", "-v", "error", "-ss", "0.5", "-i", str(out),
          "-frames:v", "1", str(f_before)])

    y_start = h - 220  # bottom band where CTA should sit
    during = _count_red(f_during, y_start, h)
    before = _count_red(f_before, y_start, h)

    assert during > 1000, f"CTA not visible for {w}x{h} (red px={during})"
    assert before < 100, f"CTA bleeding before window (red px={before})"