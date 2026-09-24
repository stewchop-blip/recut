"""SourceNormalizer (PART 5-6) — explicit normalization stage.

Every downstream pipeline stage (QuickPrep, Smart Clips, TemplateCompositor,
make_vertical) receives the same contract source:

  - SAR = 1:1 (square pixels)
  - No embedded black bars (cropped when stable)
  - Rotation either baked (transpose for 90/180/270) OR kept as autorotate
    (downstream reads rotation from probe). This minimal version keeps
    rotation as metadata (downstream handles it) and ensures SAR=1:1.

Output is a single intermediate file (normalized_source.mp4) that every
renderer treats as a standard square-pixel source.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from app.core.logging import get_logger

logger = get_logger(__name__)


@dataclass
class NormalizationResult:
    source: Path            # the normalized file (SAR 1:1, black bars cropped)
    original_input: Path    # original before any crop/normalize
    rotation: int           # rotation from original (0/90/180/270)
    sar_before: float
    sar_after: float = 1.0
    effective_before: tuple[int, int]   # (w, h) display dims before normalizing
    effective_after: tuple[int, int]
    crop_applied: bool


class SourceNormalizer:
    """Explicit normalization stage between intake and render."""

    def __init__(self) -> None:
        import shutil
        self._ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
        self._ffprobe = shutil.which("ffprobe") or "ffprobe"

    async def normalize(
        self,
        input_path: Path,
        output_path: Path,
        *,
        crop_black_bars: bool = True,
        timeout_seconds: float = 600.0,
    ) -> NormalizationResult:
        """Produce a normalized square-pixel source file.

        Pipeline (single ffmpeg pass when possible):
          1. Detect black bars (spread cropdetect sampling).
          2. Pre-crop if stable (same cropdetect used in make_vertical).
          3. Scale the result to its DISPLAY aspect ratio with SAR=1:1
             (scale=eff_w:eff_h,setsar=1). No rotation baking in this
             minimal version — downstream handles rotation via autorotate.
        """
        from app.services.media.probe import get_probe_service
        from app.services.media.ffmpeg import get_media_service
        from app.services.media.geometry import get_display_geometry

        original_input = input_path.resolve()
        source = input_path
        crop_applied = False
        rotation_before = 0
        sar_before = 1.0
        eff_before: tuple[int, int] = (0, 0)

        # 1. Probe original.
        meta = await get_probe_service().probe(input_path)
        geo = get_display_geometry(meta)
        rotation_before = geo.rotation
        sar_before = geo.sar
        eff_before = (geo.effective_width_after_rotation,
                      geo.effective_height_after_rotation)

        # 2. Black-bar crop if requested (reuses make_vertical logic).
        from app.services.media.ffmpeg import MediaService
        service = get_media_service() if False else MediaService()
        # Direct cropdetect via service method (simplified inline for now):
        bars = None
        try:
            bars = await service.detect_black_bars(input_path)
        except Exception as e:
            logger.warning("normalizer_cropdetect_failed", error=str(e)[:200])
            bars = None
        if bars is not None and crop_black_bars:
            w, h, x, y = bars
            cropped = output_path.with_suffix(".cropped.mp4")
            await service._run_crop_pass(input_path, cropped, w, h, x, y)
            source = cropped
            crop_applied = True
            # Re-probe cropped source.
            meta = await get_probe_service().probe(cropped)
            geo = get_display_geometry(meta)
            rotation_before = geo.rotation
            sar_before = geo.sar
            eff_before = (geo.effective_width_after_rotation,
                          geo.effective_height_after_rotation)

        # 3. Normalize: scale display dims with SAR=1:1.
        #    We use the effective display dims (post-rotation) as target;
        #    this produces a square-pixel video at the visual size.
        eff_w, eff_h = eff_before
        # Even dims required by H.264 (force_divisible_by=2 preserved by ffmpeg).
        scale_filter = (
            f"scale=w={eff_w}:h={eff_h}:force_divisible_by=2:setsar=1"
        )
        # For gradient/color backgrounds we don't apply here; normalization is
        # just the source file, not a composed vertical. So simple scale.
        cmd = [
            self._ffmpeg, "-y", "-v", "error",
            "-i", str(source),
            "-vf", scale_filter,
            "-c:v", "libx264", "-preset", "veryfast",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            "-c:a", "aac", "-b:a", "128k", "-ac", "2",
            str(output_path),
        ]
        import subprocess
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout_seconds)
        if proc.returncode != 0:
            raise RuntimeError(f"SourceNormalizer failed: {stderr.decode(errors='ignore')[:300]}")

        # Final probe of normalized file (must be SAR 1:1, rotation same or baked).
        meta_norm = await get_probe_service().probe(output_path)
        geo_norm = get_display_geometry(meta_norm)

        # Log audit.
        logger.info(
            "source_normalized",
            input=str(original_input),
            crop_applied=crop_applied,
            original_sar=sar_before,
            original_dar=geo.effective_width_after_rotation / max(geo.effective_height_after_rotation, 1),
            rotation_before=rotation_before,
            normalized_sar=geo_norm.sar,
            normalized_eff=(geo_norm.effective_width_after_rotation,
                             geo_norm.effective_height_after_rotation),
            output=str(output_path),
        )

        return NormalizationResult(
            source=output_path,
            original_input=original_input,
            rotation=rotation_before,
            sar_before=sar_before,
            sar_after=geo_norm.sar,
            effective_before=eff_before,
            effective_after=(geo_norm.effective_width_after_rotation,
                             geo_norm.effective_height_after_rotation),
            crop_applied=crop_applied,
        )
