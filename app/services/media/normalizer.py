"""SourceNormalizer (Phase 3-5, two-pass) — audit TZ 12-32-41.

PASS A: raw → (noautorotate if needed) → transpose bake → SAR expand →
        setsar=1 → metadata strip → normalized_base.mp4
PASS B: detect black bars ON normalized_base (coordinates now match
        pixels); stable bars → crop → normalized_source.mp4, else rename.

Contract after both passes: rotation == 0, SAR == 1:1 (audit Phase 4/5).
Crop is NEVER computed on the raw file (Phase 5: raw coordinates would be
invalid after transpose/SAR).
"""
from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass
from pathlib import Path

from app.core.logging import get_logger

logger = get_logger(__name__)


class NormalizationError(RuntimeError):
    error_code = "SOURCE_NORMALIZE_FAILED"


@dataclass(slots=True, frozen=True)
class NormalizationResult:
    source: Path                  # normalized_source.mp4
    base: Path                    # normalized_base.mp4 (post PASS A)
    rotation_before: int
    sar_before: float
    coded_before: tuple[int, int]
    base_dims: tuple[int, int]
    crop_applied: bool = False


class SourceNormalizer:
    """Two-pass: (A) rotate+SAR → base; (B) bars → crop → source."""

    def __init__(self) -> None:
        self._ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
        self._ffprobe = shutil.which("ffprobe") or "ffprobe"

    async def _probe(self, path: Path) -> tuple[int, int, float, int]:
        """(width, height, sar, rotation) of the video stream."""
        from app.services.media.probe import _parse_rotation
        proc = await asyncio.create_subprocess_exec(
            self._ffprobe, "-v", "error", "-print_format", "json",
            "-show_streams", str(path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await proc.communicate()
        if proc.returncode != 0:
            raise NormalizationError(f"probe failed: {err.decode(errors='ignore')[:200]}")
        import json
        data = json.loads(out.decode())
        v = next((s for s in data.get("streams", [])
                  if s.get("codec_type") == "video"), None)
        if v is None:
            raise NormalizationError("no video stream")
        sar_txt = v.get("sample_aspect_ratio") or "1:1"
        try:
            a, b = sar_txt.split(":")
            sar = float(a) / float(b) if float(b) else 1.0
        except (ValueError, ZeroDivisionError):
            sar = 1.0
        return (int(v.get("width") or 0), int(v.get("height") or 0),
                round(sar, 3), _parse_rotation(v))

    async def normalize(self, input_path: Path, output_path: Path, *,
                        timeout_seconds: float = 900.0) -> NormalizationResult:
        if not input_path.exists():
            raise NormalizationError(f"input missing: {input_path}")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        base = output_path.with_name(output_path.stem + "_base.mp4")

        # ---------- PASS A: rotation bake + SAR normalize ----------
        w, h, sar, rotation = await self._probe(input_path)
        vf: list[str] = []
        if rotation == 90:
            vf.append("transpose=1")
        elif rotation == 270:
            vf.append("transpose=2")
        elif rotation == 180:
            vf.append("transpose=1,transpose=1")
        if abs(sar - 1.0) > 0.01:
            vf.append(f"scale=w='trunc(iw*{sar:.6f}/2)*2':h='ih'")
        vf.append("setsar=1")

        cmd = [self._ffmpeg, "-y", "-v", "error"]
        if rotation != 0:
            cmd += ["-noautorotate"]   # we transpose explicitly (TZ Phase 19)
        cmd += ["-i", str(input_path), "-vf", ",".join(vf),
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                "-c:a", "copy", "-map_metadata", "-1", "-movflags", "+faststart",
                str(base)]
        await self._run(cmd, timeout_seconds)
        bw, bh, bsar, brot = await self._probe(base)
        if brot or abs(bsar - 1.0) > 0.02:
            raise NormalizationError(f"PASS A failed: rot={brot} sar={bsar}")

        # ---------- PASS B: black bars ON normalized_base ----------
        crop_applied = False
        bars = await self._detect_bars(base)
        if bars is not None:
            cw, ch, cx, cy = bars
            cmd2 = [self._ffmpeg, "-y", "-v", "error", "-i", str(base),
                    "-vf", f"crop={cw}:{ch}:{cx}:{cy}",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                    "-c:a", "copy", str(output_path)]
            await self._run(cmd2, timeout_seconds)
            crop_applied = True
            base.unlink(missing_ok=True)   # temp intermediate
        else:
            shutil.move(base, output_path)

        fw, fh, fsar, frot = await self._probe(output_path)
        if frot or abs(fsar - 1.0) > 0.02:
            raise NormalizationError(f"PASS B failed: rot={frot} sar={fsar}")

        res = NormalizationResult(
            source=output_path, base=base,
            rotation_before=rotation, sar_before=sar,
            coded_before=(w, h), base_dims=(bw, bh),
            crop_applied=crop_applied,
        )
        logger.info(
            "source_normalized",
            input=str(input_path),
            raw_coded=(w, h), raw_sar=sar, raw_rot=rotation,
            base_dims=(bw, bh), crop=crop_applied,
            final=(fw, fh), sar=fsar, rot=frot,
        )
        return res

    async def _run(self, cmd: list[str], timeout: float) -> None:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            _, err = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError as e:
            proc.kill()
            raise NormalizationError("normalize timed out") from e
        if proc.returncode != 0:
            raise NormalizationError(
                f"ffmpeg failed: {err.decode(errors='ignore')[:300]}")

    async def _detect_bars(self, path: Path) -> tuple[int, int, int, int] | None:
        try:
            from app.services.media.ffmpeg import MediaService
            return await MediaService().detect_black_bars(path)
        except Exception as e:
            logger.warning("normalizer_cropdetect_failed", error=str(e)[:150])
            return None