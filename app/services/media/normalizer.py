"""SourceNormalizer (Phase 3) — explicit normalization stage (audit #17-20).

Output contract: SAR 1:1, rotation BAKED into pixels (metadata removed),
black bars cropped when stable. Downstream renderers see only square-pixel
frames.

CRITICAL (audit #20): if rotation != 0, NEVER stream-copy even when SAR=1 —
pixels must actually be transposed, otherwise portrait sources render
landscape (the real stretch bug).
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
    source: Path
    rotation_before: int
    sar_before: float
    rotation_after: int = 0
    sar_after: float = 1.0
    crop_applied: bool = False
    coded_before: tuple[int, int] = (0, 0)
    coded_after: tuple[int, int] = (0, 0)


class SourceNormalizer:
    """RAW → rotate bake → SAR 1:1 → (optional) black-bar crop → normalized.mp4."""

    def __init__(self) -> None:
        import shutil
        self._ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
        self._ffprobe = shutil.which("ffprobe") or "ffprobe"

    async def probe(self, path: Path) -> tuple[dict, dict]:
        """Return (video_stream, format) json dicts."""
        proc = await asyncio.create_subprocess_exec(
            self._ffprobe, "-v", "error", "-print_format", "json",
            "-show_streams", "-show_format", str(path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await proc.communicate()
        if proc.returncode != 0:
            raise NormalizationError(f"probe failed: {err.decode(errors='ignore')[:200]}")
        import json
        data = json.loads(out.decode())
        video = next((s for s in data.get("streams", [])
                      if s.get("codec_type") == "video"), None)
        if video is None:
            raise NormalizationError("no video stream")
        return video, data.get("format", {})

    async def normalize(self, input_path: Path, output_path: Path,
                        *, crop_black_bars: bool = True,
                        timeout_seconds: float = 600.0) -> NormalizationResult:
        if not input_path.exists():
            raise NormalizationError(f"input missing: {input_path}")
        output_path.parent.mkdir(parents=True, exist_ok=True)

        video, fmt = await self.probe(input_path)
        w = int(video.get("width") or 0)
        h = int(video.get("height") or 0)
        sar_txt = video.get("sample_aspect_ratio") or "1:1"
        try:
            a, b = sar_txt.split(":")
            sar = float(a) / float(b) if float(b) else 1.0
        except (ValueError, ZeroDivisionError):
            sar = 1.0
        rotation = self._rotation(video)
        coded_before = (w, h)

        # Rotation bake (audit #19-20): transpose mapping; -noautorotate so
        # ffmpeg does NOT rotate twice. 180 = transpose twice.
        vf_parts: list[str] = []
        if rotation == 90:      # displaymatrix 90 = shot rotated right
            vf_parts.append("transpose=1")
        elif rotation == 270:
            vf_parts.append("transpose=2")
        elif rotation == 180:
            vf_parts.append("transpose=1,transpose=1")
        # SAR normalization: expand coded pixels to display size, then square.
        if abs(sar - 1.0) > 0.01:
            vf_parts.append(f"scale=w='trunc(iw*{sar:.6f}/2)*2':h='ih'")
        vf_parts.append("setsar=1")

        # Black-bar crop (stable detection shared with make_vertical).
        crop_applied = False
        if crop_black_bars:
            bars = await self._detect_bars(input_path)
            if bars is not None:
                cw, ch, cx, cy = bars
                vf_parts.append(f"crop={cw}:{ch}:{cx}:{cy}")
                crop_applied = True

        # Bake rotation into pixels and DROP rotation metadata (audit #18):
        # remux with -noautorotate input option + removemetatags for rotate.
        cmd = [self._ffmpeg, "-y", "-v", "error"]
        if rotation != 0:
            # decode WITHOUT autorotate; we transpose explicitly
            cmd += ["-noautorotate"]
        cmd += ["-i", str(input_path), "-vf", ",".join(vf_parts),
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                "-c:a", "copy", "-map_metadata", "-1", "-movflags", "+faststart",
                str(output_path)]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            _, err = await asyncio.wait_for(proc.communicate(), timeout_seconds)
        except asyncio.TimeoutError as e:
            proc.kill()
            raise NormalizationError("normalize timed out") from e
        if proc.returncode != 0:
            raise NormalizationError(
                f"ffmpeg failed: {err.decode(errors='ignore')[:300]}")

        # Verify (audit #18: probe again after normalization).
        v2, _ = await self.probe(output_path)
        rot_after = self._rotation(v2)
        sar_txt2 = v2.get("sample_aspect_ratio") or "1:1"
        try:
            a2, b2 = sar_txt2.split(":")
            sar_after = float(a2) / float(b2) if float(b2) else 1.0
        except (ValueError, ZeroDivisionError):
            sar_after = 1.0
        if rot_after := self._rotation(v2):
            # metadata slipped through — second pass to strip rotate tag
            strip = output_path.with_suffix(".strip.mp4")
            await self._strip_rotation(output_path, strip)
            shutil.move(strip, output_path)
            v3, _ = await self.probe(output_path)
            rot_after = self._rotation(v3)
        if rot_after or abs(sar_after - 1.0) > 0.02:
            raise NormalizationError(
                f"post-normalize check failed: rot={rot_after} sar={sar_after}")

        res = NormalizationResult(
            source=output_path,
            rotation_before=rotation,
            sar_before=sar,
            rotation_after=rot_after,
            sar_after=sar_after,
            crop_applied=crop_applied,
            coded_before=coded_before,
            coded_after=(int(v2.get("width") or 0), int(v2.get("height") or 0)),
        )
        logger.info(
            "source_normalized",
            input=str(input_path), rotation=rotation, sar=round(sar, 3),
            coded=coded_before, after=res.coded_after, crop=crop_applied,
        )
        return res

    async def _detect_bars(self, path: Path) -> tuple[int, int, int, int] | None:
        """Reuse MediaService.detect_black_bars (stable spread sampling)."""
        try:
            from app.services.media.ffmpeg import MediaService
            return await MediaService().detect_black_bars(path)
        except Exception as e:
            logger.warning("normalizer_cropdetect_failed", error=str(e)[:150])
            return None

    async def _strip_rotation(self, src: Path, dst: Path) -> None:
        proc = await asyncio.create_subprocess_exec(
            self._ffmpeg, "-y", "-v", "error", "-i", str(src),
            "-c", "copy", "-map_metadata", "-1",
            "-metadata:s:v", "rotate=0",
            "-bsf:v", "h264_metadata=display_matrix=delete",
            str(dst))
        _, err = await proc.communicate()
        if proc.returncode != 0:
            raise NormalizationError("strip rotation failed")

    @staticmethod
    def _rotation(video: dict) -> int:
        """0/90/180/270 from tags.rotate OR displaymatrix (probe._parse_rotation)."""
        from app.services.media.probe import _parse_rotation
        return _parse_rotation(video)