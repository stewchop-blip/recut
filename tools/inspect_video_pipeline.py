"""TZ 12-32-41 PHASE 7 — pipeline geometry inspector.

Usage: python tools/inspect_video_pipeline.py INPUT [--render]

Saves debug/01_raw_frame.jpg ... 04_composed.jpg + geometry.json with
RAW / NORMALIZED_BASE / NORMALIZED_CROP / COMPOSITOR / FINAL geometry.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

FF = "ffmpeg"
FP = "ffprobe"


def probe(path: Path) -> dict:
    out = subprocess.run(
        [FP, "-v", "error", "-print_format", "json", "-show_streams", str(path)],
        capture_output=True, text=True, check=True).stdout
    v = next(s for s in json.loads(out)["streams"] if s["codec_type"] == "video")
    sar_txt = v.get("sample_aspect_ratio") or "1:1"
    try:
        a, b = sar_txt.split(":")
        sar = round(float(a) / float(b), 3) if float(b) else 1.0
    except (ValueError, ZeroDivisionError):
        sar = 1.0
    w, h = int(v.get("width") or 0), int(v.get("height") or 0)
    rot = _rotation(v)
    eff_w, eff_h = (h, w) if rot in (90, 270) else (w, h)
    return {"width": w, "height": h, "sar": sar,
            "dar": v.get("display_aspect_ratio"), "rotation": rot,
            "effective_width": eff_w, "effective_height": eff_h}


def _rotation(v: dict) -> int:
    from app.services.media.probe import _parse_rotation
    return _parse_rotation(v)


def frame(path: Path, out: Path, t: str = "0.5") -> None:
    subprocess.run([FF, "-y", "-v", "error", "-ss", t, "-i", str(path),
                    "-frames:v", "1", str(out)], check=True)


async def inspect(src: Path, debug: Path, render: bool) -> dict:
    from app.services.media.normalizer import SourceNormalizer
    from app.services.overlays.compositor import TemplateSpec
    debug.mkdir(parents=True, exist_ok=True)
    g: dict = {}

    g["RAW"] = probe(src)
    frame(src, debug / "01_raw_frame.jpg")

    base = debug / "normalized_base.mp4"
    src_n = debug / "normalized_source.mp4"
    norm = await SourceNormalizer().normalize(src, src_n)
    # keep a copy of base for probing (normalizer deletes it after crop)
    if norm.crop_applied:
        base_tmp = debug / "normalized_base.mp4"
        subprocess.run([FF, "-y", "-v", "error", "-i", str(src),
                        "-vf", "setsar=1",
                        "-c:v", "libx264", "-preset", "veryfast",
                        "-an", str(base_tmp)], check=True)
        g["NORMALIZED_BASE"] = probe(base_tmp)
    else:
        g["NORMALIZED_BASE"] = g["NORMALIZED_CROP"] = probe(src_n)
    g["NORMALIZED_CROP"] = probe(src_n)
    frame(src_n, debug / "02_normalized_base.jpg" if not norm.crop_applied
          else debug / "03_normalized_crop.jpg")

    g["COMPOSITOR"] = {
        "canvas": [1080, 1920],
        "video_box": [1080, 1440, 0, 192],   # TemplateSpec default
        "foreground_ratio": round(
            g["NORMALIZED_CROP"]["effective_width"]
            / max(g["NORMALIZED_CROP"]["effective_height"], 1), 4),
    }

    g["FINAL"] = g["NORMALIZED_CROP"]
    frame(src_n, debug / "04_composed.jpg")

    (debug / "geometry.json").write_text(
        json.dumps(g, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(g, indent=2, ensure_ascii=False))

    if render:
        out = debug / "composed.mp4"
        from app.pipeline.quick_prep import QuickPrepPipeline
        res = await QuickPrepPipeline().run(
            src_n, debug, target_width=1080, target_height=1920,
            target_fps=30, video_bitrate="4000k", audio_bitrate="128k",
            cta_asset=None, cta_position="bottom", cta_mode="tail",
            cta_duration_seconds=2.0, cta_start_seconds=0.0,
            cta_min_margin_px=40, output_width=1080, output_height=1920)
        g["FINAL"] = probe(res.final_path)
        (debug / "geometry.json").write_text(
            json.dumps(g, indent=2, ensure_ascii=False), encoding="utf-8")
        print("FINAL:", g["FINAL"])
    return g


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("--render", action="store_true")
    args = ap.parse_args()
    src = Path(args.input)
    debug = src.parent / "debug"
    asyncio.run(inspect(src, debug, args.render))