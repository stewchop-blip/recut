"""QuickPrep pipeline — fast FFmpeg-only path for short videos.

Used when the user has already-edited footage and just wants the
'one-tap' repackage: vertical format + CTA + clean export.

Pipeline (no Whisper, no LLM, no clipping, no subtitles):

  video
   -> ffprobe
   -> validation (size, duration, has-audio)
   -> vertical format (1080x1920 if landscape/square, passthrough if portrait)
   -> CTA overlay (if enabled)
   -> audio normalization
   -> clean metadata export
   -> ffprobe verification
   -> send to Telegram

If any step fails, we fall through to whatever was produced last —
we always return *something* playable.
"""
from dataclasses import dataclass
from pathlib import Path

from app.core.logging import get_logger
from app.services.media import get_media_service, get_probe_service

logger = get_logger(__name__)


class QuickPrepError(RuntimeError):
    """Quick Prep pipeline failed completely."""


@dataclass(slots=True, frozen=True)
class QuickPrepResult:
    """One ready-to-publish clip."""
    final_path: Path
    size_bytes: int
    width: int
    height: int
    duration_seconds: float
    has_cta: bool


class QuickPrepPipeline:
    """Stage orchestrator for the FFmpeg-only path."""

    def __init__(self) -> None:
        # Lazy — services are loaded on first use so they don't pin ffmpeg at import.
        self._media = None
        self._probe = None

    async def run(
        self,
        input_video: Path,
        job_dir: Path,
        *,
        target_width: int,
        target_height: int,
        target_fps: int,
        video_bitrate: str,
        audio_bitrate: str,
        cta_asset: Path | None,
        cta_position: str,
        cta_mode: str,
        cta_duration_seconds: float,
        cta_start_seconds: float,
        cta_min_margin_px: int,
        output_width: int,
        output_height: int,
        cta_size_preset: str = "medium",
        cta_overlay_type: str = "png",
        background_id: str = "blur",
        title_text: str = "",
        brand_corner: bool = False,
        audio_preset: str = "original",
    ) -> QuickPrepResult:
        media = self._media or get_media_service()
        probe = self._probe or get_probe_service()
        self._media = media
        self._probe = probe

        if not input_video.exists():
            raise QuickPrepError(f"Source video missing: {input_video}")

        job_dir.mkdir(parents=True, exist_ok=True)

        # 1. Probe.
        try:
            meta = await probe.probe(input_video)
        except Exception as e:
            raise QuickPrepError(f"Probe failed: {e}") from e

        logger.info(
            "video_geometry_input",
            input=str(input_video),
            width=meta.width,
            height=meta.height,
            sar=meta.sample_aspect_ratio,
            dar=meta.display_aspect_ratio,
            rotation=meta.rotation,
        )

        if meta.duration_seconds <= 0:
            raise QuickPrepError("Source has no duration")
        if not meta.has_audio:
            logger.warning("quickprep_no_audio", path=str(input_video))

        # 2. Black-bar detection BEFORE portrait check (a "9:16" video with
        # embedded letterbox must be pre-cropped, not passed through).
        try:
            bars = await media.detect_black_bars(input_video)
        except Exception as e:
            logger.warning("quickprep_cropdetect_failed", error=str(e)[:200])
            bars = None
        if bars is not None:
            w, h, x, y = bars
            logger.info("black_bars_detected", crop_w=w, crop_h=h, crop_x=x, crop_y=y)
            cropped = job_dir / "precrop.mp4"
            await media._run_crop_pass(input_video, cropped, w, h, x, y)
            input_video = cropped
            try:
                meta = await probe.probe(input_video)
            except Exception as e:
                raise QuickPrepError(f"Re-probe after crop failed: {e}") from e

        # 3. Vertical format — decisions on EFFECTIVE DISPLAY GEOMETRY
        # (PART 5/6/7): coded × SAR, rotation applied. Never coded dims.
        from app.services.media.geometry import get_display_geometry, is_near_aspect
        geo = get_display_geometry(meta)
        current = input_video
        # Already 9:16 (display ratio within 5%) → passthrough, but normalize
        # SAR (preserving DAR) and rotation metadata if present.
        target_ratio = target_width / max(target_height, 1)
        try:
            if is_near_aspect(meta, target_ratio, tolerance=0.05):
                vertical_path = job_dir / "vertical.mp4"
                import shutil
                needs_norm = (
                    abs(meta.sample_aspect_ratio - 1.0) > 0.01
                    or geo.rotation in (90, 180, 270)
                )
                if needs_norm:
                    await get_media_service().normalize_square_pixels(
                        current, vertical_path,
                    )
                    logger.info(
                        "quickprep_passthrough_normalized_sar",
                        path=str(vertical_path),
                        source_sar=meta.sample_aspect_ratio,
                        source_rotation=geo.rotation,
                    )
                else:
                    shutil.copy2(current, vertical_path)
                    logger.info("quickprep_passthrough_vertical", path=str(vertical_path))
            else:
                vertical_path = job_dir / "vertical.mp4"
                await media.make_vertical(
                    current,
                    vertical_path,
                    target_width=target_width,
                    target_height=target_height,
                    target_fps=target_fps,
                    video_bitrate=video_bitrate,
                    audio_bitrate=audio_bitrate,
                    background_id=background_id,
                    title_text=title_text,
                    brand_corner=brand_corner,
                    audio_preset=audio_preset,
                )
            current = vertical_path
            # Output geometry log (audit #22).
            try:
                out_meta = await probe.probe(vertical_path)
                logger.info(
                    "video_geometry_output",
                    width=out_meta.width, height=out_meta.height,
                    sar=out_meta.sample_aspect_ratio,
                    dar=out_meta.display_aspect_ratio,
                )
            except Exception:
                pass
        except Exception as e:
            # PART 9: NEVER silently fall back to a deformed source —
            # a geometry/render failure must fail the job clearly.
            logger.error("quickprep_vertical_failed_no_fallback", error=str(e)[:300])
            raise QuickPrepError(f"Vertical render failed: {e}") from e

        # 3. CTA overlay — ONLY the user's asset. No default "Recut"
        # placeholder: no user banner → no overlay (audit: no stubs).
        has_cta = False
        effective_cta_asset: Path | None = None
        if cta_asset is not None and cta_asset.exists():
            effective_cta_asset = cta_asset
        elif cta_asset is not None:
            logger.warning("cta_asset_missing_no_fallback", path=str(cta_asset))
        if effective_cta_asset is not None and effective_cta_asset.exists():
            from app.services.overlays.cta import CTAService, CTAOverlaySpec
            spec = CTAService()._make_spec(
                clip_duration=meta.duration_seconds,
                mode=cta_mode,
                duration_seconds=cta_duration_seconds,
                start_seconds=cta_start_seconds,
                position=cta_position,
                margin=cta_min_margin_px,
                output_w=meta.width,
                output_h=meta.height,
                asset=effective_cta_asset,
            )
            if spec is not None:
                cta_path = job_dir / "with_cta.mp4"
                try:
                    await media.burn_cta(
                        current, spec.asset_path, cta_path,
                        position=cta_position,
                        margin=cta_min_margin_px,
                        start_seconds=spec.start_seconds,
                        end_seconds=spec.end_seconds,
                        size_preset=cta_size_preset,
                        overlay_type=cta_overlay_type,
                    )
                    current = cta_path
                    has_cta = True
                except Exception as e:
                    logger.error(
                        "quickprep_cta_failed",
                        job_id=getattr(locals().get("pending"), "job_id", None),
                        error=str(e)[:300],
                        cta_path=str(effective_cta_asset),
                    )
                    raise QuickPrepError(f"CTA overlay failed: {e}") from e

        # 4. Clean final export (loudnorm + strip metadata).
        final_path = job_dir / "final.mp4"
        try:
            await media.finalize_export(current, final_path)
            current = final_path
        except Exception as e:
            logger.warning("quickprep_finalize_failed_using_previous", error=str(e)[:200])
            if not current.exists():
                raise QuickPrepError(f"No output produced: {e}") from e

        # 5. Verify + GEOMETRY VALIDATION (PART 9): never send a deformed
        # clip — output SAR must be 1:1 and dims must match the canvas.
        try:
            final_meta = await probe.probe(current)
            if abs(final_meta.sample_aspect_ratio - 1.0) > 0.01:
                raise QuickPrepError(
                    f"GeometryValidationError: output SAR="
                    f"{final_meta.sample_aspect_ratio}, expected 1:1 "
                    f"(would display stretched) — not sending"
                )
            if (output_width, output_height) != (final_meta.width, final_meta.height) \
                    and (final_meta.width, final_meta.height) != (meta.effective_width, meta.effective_height):
                logger.warning(
                    "geometry_output_dims_unexpected",
                    expected=f"{output_width}x{output_height}",
                    got=f"{final_meta.width}x{final_meta.height}",
                    source_effective=f"{meta.effective_width}x{meta.effective_height}",
                )
            logger.info(
                "video_geometry_final",
                source_coded=f"{meta.coded_width}x{meta.coded_height}",
                source_effective=f"{meta.effective_width}x{meta.effective_height}",
                source_sar=meta.sample_aspect_ratio,
                source_dar=meta.display_aspect_ratio,
                source_rotation=meta.rotation,
                output=f"{final_meta.width}x{final_meta.height}",
                output_sar=final_meta.sample_aspect_ratio,
                output_dar=final_meta.display_aspect_ratio,
            )
            return QuickPrepResult(
                final_path=current,
                size_bytes=current.stat().st_size,
                width=final_meta.width,
                height=final_meta.height,
                duration_seconds=final_meta.duration_seconds,
                has_cta=has_cta,
            )
        except Exception as e:
            logger.warning("quickprep_verify_failed", error=str(e)[:200])
            return QuickPrepResult(
                final_path=current,
                size_bytes=current.stat().st_size,
                width=meta.width,
                height=meta.height,
                duration_seconds=meta.duration_seconds,
                has_cta=has_cta,
            )