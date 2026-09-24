"""Real-geometry diagnostic tool (PART 8 of the stabilization TZ).

Usage:
    python tools/debug_geometry.py input.mp4 [--t 1.0] [--out debug]

Produces (in --out dir, default ./debug):
    geometry.json      — SOURCE / TEMPLATE / FOREGROUND / FINAL geometry
    source.jpg         — source frame at --t (square-pixel, rotation applied)
    normalized.jpg     — pre-cropped frame (black bars removed)
    final.jpg          — composed 1080x1920 vertical frame

One look at final.jpg tells you whether a real video stretches; the JSON
tells you at which stage the aspect got distorted.
"""
import argparse
import asyncio
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.media.geometry import get_display_geometry  # noqa: E402
from app.services.media.probe import get_probe_service  # noqa: E402
from app.services.overlays.compositor import TemplateSpec  # noqa: E402


def _ffmpeg() -> str:
    import shutil
    return shutil.which("ffmpeg") or "ffmpeg"


def extract(src: Path, t: float, out: Path, vf: str | None = None) -> None:
    cmd = [_ffmpeg(), "-y", "-v", "error", "-ss", str(t), "-i", str(src),
           "-frames:v", "1"]
    if vf:
        cmd += ["-vf", vf]
    cmd += [str(out)]
    subprocess.run(cmd, check=True)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("--t", type=float, default=1.0)
    ap.add_argument("--out", default="debug")
    ap.add_argument("--canvas", default="1080x1920")
    args = ap.parse_args()

    src = Path(args.input)
    if not src.exists():
        print(f"not found: {src}")
        sys.exit(1)
    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    W, H = (int(x) for x in args.canvas.split("x"))

    report: dict = {}

    # 1. SOURCE raw geometry.
    probe = get_probe_service()
    meta = await probe.probe(src)
    geo = get_display_geometry(meta)
    report["source_raw"] = {
        "coded": [meta.coded_width, meta.coded_height],
        "sar": geo.sar,
        "dar": geo.dar,
        "rotation": geo.rotation,
        "display_before_rotation": [geo.display_width_before_rotation,
                                    geo.display_height_before_rotation],
        "effective_after_rotation": [geo.effective_width_after_rotation,
                                     geo.effective_height_after_rotation],
    }

    # source.jpg — as the player shows it (square pixels, autorotated).
    extract(src, args.t, outdir / "source.jpg",
            "scale=iw*sar:ih,setsar=1" if abs(geo.sar - 1.0) > 0.01 else None)

    # 2. NORMALIZED (black bars removed, SAR reset via explicit scaling).
    ratio = geo.effective_width_after_rotation / max(geo.effective_height_after_rotation, 1)
    spec = TemplateSpec(W, H)
    fg = spec.fit_video(ratio)
    report["template"] = {"canvas": [W, H]}
    report["foreground"] = {"x": fg.x, "y": fg.y, "width": fg.width,
                            "height": fg.height,
                            "dar": round(ratio, 4)}

    # normalized.jpg — the fg box rendered alone (what compose would place).
    norm = outdir.parent / "_norm_tmp.mp4" if False else outdir / "normalized.jpg"
    extract(src, args.t, norm,
            f"scale={fg.width}:{fg.height},setsar=1")

    # 3. FINAL — one-frame compose: solid bg + fg at box position.
    final_png = outdir / "final.jpg"
    fc = (f"color=c=black:s={W}x{H}:d=0.04[bg];"
          f"[0:v]scale={fg.width}:{fg.height},setsar=1[fg];"
          f"[bg][fg]overlay={fg.x}:{fg.y}:shortest=1[v]")
    subprocess.run([_ffmpeg(), "-y", "-v", "error", "-ss", str(args.t),
                    "-i", str(src), "-filter_complex", fc, "-map", "[v]",
                    "-frames:v", "1", str(final_png)], check=True)

    (outdir / "geometry.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    print(f"\nframes: {outdir}\\source.jpg normalized.jpg final.jpg")


if __name__ == "__main__":
    asyncio.run(main())