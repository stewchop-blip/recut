"""BLOCKING regression (audit #59-60): rotation bake + SAR normalize."""
import asyncio
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.media.normalizer import SourceNormalizer, NormalizationError


def _ffmpeg() -> str:
    import shutil
    return shutil.which("ffmpeg") or "ffmpeg"


def _probe(path: Path) -> tuple[int, int, float, int]:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_streams", str(path)], capture_output=True, text=True, check=True)
    import json
    d = json.loads(r.stdout)
    v = next(s for s in d["streams"] if s["codec_type"] == "video")
    sar = 1.0
    try:
        a, b = (v.get("sample_aspect_ratio") or "1:1").split(":")
        sar = float(a) / float(b) if float(b) else 1.0
    except (ValueError, ZeroDivisionError):
        sar = 1.0
    rot = 0
    tags = v.get("tags") or {}
    if tags.get("rotate"):
        rot = int(float(tags["rotate"])) % 360
    for sd in v.get("side_data_list") or []:
        if sd.get("rotation") is not None:
            rot = (int(float(sd["rotation"])) * -1) % 360
    return int(v["width"]), int(v["height"]), round(sar, 3), rot


def make_rot90_fixture(out: Path) -> None:
    # coded 1920x1080 landscape, metadata rotation 90 → VISUAL portrait.
    # transpose=2 bakes the 90° counter-clockwise rotation into pixels.
    subprocess.run([
        _ffmpeg(), "-y", "-v", "error",
        "-f", "lavfi", "-i", "testsrc2=s=1920x1080:d=2",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out)], check=True)
    # attach 90° rotation metadata (displaymatrix) — INPUT option
    tagged = out.with_name(out.stem + "_tagged.mp4")
    subprocess.run([
        _ffmpeg(), "-y", "-v", "error", "-display_rotation", "90",
        "-i", str(out), "-c", "copy", str(tagged)], capture_output=True)
    return tagged


async def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="normtest_"))
    src = make_rot90_fixture(tmp / "rot90.mp4")
    w0, h0, sar0, rot0 = _probe(src)
    print(f"RAW: coded={w0}x{h0} sar={sar0} rot={rot0}")

    n = SourceNormalizer()
    out = tmp / "normalized.mp4"
    res = await n.normalize(src, out)
    w1, h1, sar1, rot1 = _probe(out)
    print(f"NORM: coded={w1}x{h1} sar={sar1} rot={rot1} crop={res.crop_applied}")
    ok = (w1 < h1) and abs(sar1 - 1.0) <= 0.02 and rot1 == 0
    print("TEST 59 (rot90 → portrait, SAR 1:1, rot 0):", "PASS" if ok else "FAIL")

    # TEST 60: normal portrait untouched (no deformation).
    p = tmp / "portrait.mp4"
    subprocess.run([
        _ffmpeg(), "-y", "-v", "error", "-f", "lavfi",
        "-i", "testsrc2=s=1080x1920:d=2",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(p)], check=True)
    out2 = tmp / "portrait_norm.mp4"
    await n.normalize(p, out2, crop_black_bars=False)
    w2, h2, sar2, rot2 = _probe(out2)
    ok2 = (w2 < h2) and abs(sar2 - 1.0) <= 0.02 and rot2 == 0
    print(f"TEST 60 (1080x1920 stays portrait): {w2}x{h2} sar={sar2} rot={rot2} ->",
          "PASS" if ok2 else "FAIL")
    sys.exit(0 if (ok and ok2) else 1)


if __name__ == "__main__":
    asyncio.run(main())