"""PART 10 — REAL geometry regression tests (anamorphic, rotation, SAR).

Sources are synthetic test patterns (circle + square + grid) rendered to
real MP4s and processed through the REAL pipeline. A test fails unless:

- output SAR is 1:1,
- the circle is still a circle (bbox aspect ≈ 1) in the DISPLAYED frame,
- the square is still a square.

Plain "resolution == 1080x1920" is NOT considered success.
"""
from __future__ import annotations

import asyncio
import subprocess
import tempfile
from pathlib import Path

import pytest

W, H = 1080, 1920  # default canvas


def _make_pattern(width: int, height: int) -> "Image":  # noqa: F821
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (width, height), (10, 10, 10))
    d = ImageDraw.Draw(img)
    # grid
    for x in range(0, width, width // 12):
        d.line([(x, 0), (x, height)], fill=(60, 60, 60), width=1)
    for y in range(0, height, height // 12):
        d.line([(0, y), (width, y)], fill=(60, 60, 60), width=1)
    # circle (red), square (green)
    r = min(width, height) // 6
    d.ellipse([width // 4 - r, height // 4 - r, width // 4 + r, height // 4 + r], fill=(230, 30, 30))
    s = min(width, height) // 4
    d.rectangle([width * 3 // 4 - s // 2, height // 4 - s // 2,
                 width * 3 // 4 + s // 2, height // 4 + s // 2], fill=(30, 220, 30))
    return img


def _ffmpeg() -> str:
    """Same ffmpeg MediaService resolves (pytest env may lack PATH entry)."""
    import shutil
    return shutil.which("ffmpeg") or "ffmpeg"


def _encode(img, out: Path, *, sar: str | None = None, display_rotation: int | None = None) -> Path:
    """Encode a 1s static-pattern MP4 with optional SAR / display matrix."""
    from PIL import Image
    raw = out.with_suffix(".raw")
    raw.write_bytes(img.tobytes())
    cmd = [
        _ffmpeg(), "-y", "-v", "error",
    ]
    if display_rotation is not None:
        # -display_rotation is an INPUT option — must precede -i.
        cmd += ["-display_rotation", str(display_rotation)]
    cmd += [
        "-f", "rawvideo", "-pix_fmt", "rgb24",
        "-s", f"{img.width}x{img.height}", "-r", "25",
        "-i", str(raw),
    ]
    vf = []
    if sar:
        vf.append(f"setsar={sar}")
    cmd += ["-vf", ",".join(vf)] if vf else []
    cmd += [
        "-t", "1", "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-movflags", "+faststart", str(out),
    ]
    subprocess.run(cmd, check=True)
    raw.unlink()
    return out


def _probe_dims(path: Path) -> dict:
    import json
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", str(path)],
        capture_output=True, text=True, check=True,
    )
    s = next(x for x in json.loads(r.stdout)["streams"] if x.get("codec_type") == "video")
    return {
        "w": int(s["width"]), "h": int(s["height"]),
        "sar": s.get("sample_aspect_ratio", "1:1"),
        "dar": s.get("display_aspect_ratio", ""),
    }


def _extract_frame(path: Path, t: float, out: Path) -> "Image":  # noqa: F821
    # NOTE: static test pattern — frame 0 == any frame; -ss on 1s
    # rawvideo-encoded files can seek past all frames (empty output).
    subprocess.run(
        [_ffmpeg(), "-y", "-v", "error", "-i", str(path),
         "-frames:v", "1", str(out)],
        check=True,
    )
    from PIL import Image
    return Image.open(out).convert("RGB")


def _bbox(img, pred):
    """Bounding box of pixels matching pred as (min_x, min_y, max_x, max_y) or None."""
    px = img.load()
    w, h = img.size
    minx, miny, maxx, maxy = None, None, None, None
    for y in range(0, h, 2):
        for x in range(0, w, 2):
            if pred(*px[x, y]):
                if minx is None or x < minx: minx = x
                if maxx is None or x > maxx: maxx = x
                if miny is None or y < miny: miny = y
                if maxy is None or y > maxy: maxy = y
    if minx is None:
        return None
    return (minx, miny, maxx, maxy)


def _shape_ok(img, rgb, tol=0.12):
    """bbox aspect of a colored shape must be ~1 (circle stays circle,
    square stays square) AND present."""
    r, g, b = rgb
    pred = (lambda pr, pg, pb: abs(pr - r) < 50 and abs(pg - g) < 50 and abs(pb - b) < 50)
    bb = _bbox(img, pred)
    if bb is None:
        return False, None
    bw, bh = bb[2] - bb[0], bb[3] - bb[1]
    if bh == 0:
        return False, bw / max(bh, 1)
    return abs(bw / bh - 1.0) < tol, bw / bh


def _is_sar_square(sar: str) -> bool:
    if ":" not in sar:
        return False
    a, b = sar.split(":")
    try:
        return abs(float(a) / float(b) - 1.0) < 0.02
    except ZeroDivisionError:
        return False


async def _render(src: Path, out: Path, **kw):
    from app.services.media.ffmpeg import MediaService
    await MediaService().make_vertical(src, out, background_id="dark", **kw)


def _run(coro):
    import asyncio
    return asyncio.run(coro)


def test_anamorphic_sar_16_15_no_stretch():
    """720x576 SAR 16:15 (display 768x576) — fg must NOT stretch."""
    d = Path(tempfile.mkdtemp())
    src = _encode(_make_pattern(720, 576), d / "anam.mp4", sar="16/15")
    meta = _probe_dims(src)
    assert meta["sar"] == "16:15", "fixture must be anamorphic"
    out = d / "out.mp4"
    asyncio.run(_render(src, out))
    m = _probe_dims(out)
    assert _is_sar_square(m["sar"]), f"output SAR must be 1:1, got {m['sar']}"
    assert (m["w"], m["h"]) == (W, H)
    frame = _extract_frame(out, 0.5, d / "f.png")
    ok_c, ratio_c = _shape_ok(frame, (230, 30, 30))
    ok_s, ratio_s = _shape_ok(frame, (30, 220, 30))
    assert ok_c, f"circle deformed (bbox ratio {ratio_c})"
    assert ok_s, f"square deformed (bbox ratio {ratio_s})"


def test_landscape_16_9_square_sar_no_stretch():
    """1920x1080 SAR 1:1 — fg 1080x~608, circle/square intact."""
    d = Path(tempfile.mkdtemp())
    src = _encode(_make_pattern(1920, 1080), d / "land.mp4")
    out = d / "out.mp4"
    asyncio.run(_render(src, out))
    m = _probe_dims(out)
    assert _is_sar_square(m["sar"])
    frame = _extract_frame(out, 0.5, d / "f.png")
    ok_c, ratio_c = _shape_ok(frame, (230, 30, 30))
    ok_s, ratio_s = _shape_ok(frame, (30, 220, 30))
    assert ok_c, f"circle deformed (bbox ratio {ratio_c})"
    assert ok_s, f"square deformed (bbox ratio {ratio_s})"


def test_passthrough_anamorphic_vertical_normalized():
    """1080x1920 SAR 9:10-coded... use 576x1024 SAR 2:3 (display 9:16) —
    passthrough must normalize SAR preserving DAR, circle stays circle."""
    d = Path(tempfile.mkdtemp())
    src = _encode(_make_pattern(576, 1024), d / "src.mp4", sar="3/4")
    src = _encode(_make_pattern(512, 1024), d / "src.mp4", sar="9/8")
    meta = _probe_dims(src)
    assert meta["dar"].startswith("9:16"), f"fixture DAR {meta['dar']}"
    out = d / "out.mp4"
    from app.services.media.ffmpeg import MediaService
    asyncio.run(MediaService().normalize_square_pixels(src, out))
    m = _probe_dims(out)
    assert _is_sar_square(m["sar"]), f"SAR not normalized: {m['sar']}"
    # display ratio preserved
    ratio = m["w"] / m["h"]
    assert abs(ratio - 0.5625) < 0.02, f"display ratio changed: {ratio}"
    frame = _extract_frame(out, 0.5, d / "f.png")
    ok_c, ratio_c = _shape_ok(frame, (230, 30, 30))
    assert ok_c, f"circle deformed after normalization (ratio {ratio_c})"


def test_displaymatrix_rotation_90():
    """Landscape-coded file with display matrix rotation 90 → effective
    portrait; output must be 9:16 with rotation applied, SAR 1:1."""
    d = Path(tempfile.mkdtemp())
    src = _encode(_make_pattern(1280, 720), d / "rot.mp4", display_rotation=90)
    out = d / "out.mp4"
    asyncio.run(_render(src, out))
    m = _probe_dims(out)
    assert _is_sar_square(m["sar"])
    assert (m["w"], m["h"]) == (W, H)
    frame = _extract_frame(out, 0.5, d / "f.png")
    ok_c, ratio_c = _shape_ok(frame, (230, 30, 30))
    ok_s, ratio_s = _shape_ok(frame, (30, 220, 30))
    assert ok_c, f"circle deformed (bbox ratio {ratio_c})"
    assert ok_s, f"square deformed (bbox ratio {ratio_s})"


def test_geometry_helpers():
    from app.services.media.geometry import fit_inside, is_near_aspect
    class M:
        effective_width = 768
        effective_height = 576
        sample_aspect_ratio = 16 / 15
    assert is_near_aspect(M(), 4 / 3, 0.02)
    assert not is_near_aspect(M(), 9 / 16, 0.05)
    assert fit_inside(4 / 3, 1080, 1920) == (1080, 810)
    assert fit_inside(9 / 16, 1080, 1920) == (1080, 1920)
    assert fit_inside(16 / 9, 1080, 1920) == (1080, 608)
