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
        transformation_preset: str = "custom",  # PART 23: clean / meme / brand / custom
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

        # 2b. MANDATORY normalization (TZ Phase 3/4/5/6): EVERY source goes
        # raw → SourceNormalizer (two-pass: rotate+SAR, then bar crop on
        # normalized pixels) → normalized_source. Pre-normalization
        # cropdetect here is REMOVED (single source of truth).
        from app.services.media.normalizer import SourceNormalizer
        normalized_path = job_dir / "normalized_source.mp4"
        norm = await SourceNormalizer().normalize(input_video, normalized_path)
        input_video = norm.source
        logger.info(
            "quickprep_source_normalized",
            raw_coded=norm.coded_before, raw_rot=norm.rotation_before,
            raw_sar=norm.sar_before, base=norm.base_dims,
            crop=norm.crop_applied,
        )
        try:
            meta = await probe.probe(input_video)
        except Exception as e:
            raise QuickPrepError(f"Re-probe after normalize failed: {e}") from e

        # 2c. TransformationPreset resolution (PART 23-24) — single settings object.
        # Overrides per-style settings when using a built-in preset.
        from app.services.overlays.presets import resolve_preset
        preset_cfg = resolve_preset(transformation_preset)
        if preset_cfg is not None:
            # Apply preset's visual/audio settings; keep user banner config
            # (cta_size overrides only when preset specifies different size).
            if preset_cfg.background_id != "blur" or preset_cfg.name in ("clean", "meme", "brand"):
                background_id = preset_cfg.background_id
            # Title: for meme/brand use preset title id; custom uses _resolve_title_text(s)
            if preset_cfg.title_id != "none":
                from app.services.overlays.templates import TITLES
                title_text = TITLES.get(preset_cfg.title_id, TITLES.get("none")).text
            else:
                title_text = ""
            brand_corner = preset_cfg.brand_corner
            audio_preset = preset_cfg.audio_preset
        speed = getattr(preset_cfg, "speed", 1.0) if preset_cfg is not None else 1.0
        color_preset = getattr(preset_cfg, "color_preset", "original") if preset_cfg is not None else "original"
        layout_id = getattr(preset_cfg, "layout_id", "pip") if preset_cfg is not None else "pip"
        # TZ Phase 19: log what actually applied (verify preset worked).
        logger.info(
            "transformations_applied",
            preset=transformation_preset,
            layout=layout_id,
            background=background_id,
            speed=speed,
            color=color_preset,
            title=bool(title_text),
            brand_corner=brand_corner,
            banner_size=cta_size_preset,
            audio=audio_preset,
        )
        # (cta_size remains from user DB settings unless preset explicitly
        # overrides — kept at user value for simplicity.)

        # 3. Style rendering — ALWAYS through make_vertical/TemplateCompositor
        # (TZ Phase 10/11): normalization != style rendering. A vertical
        # source no longer bypasses background/title/brand/banner layout.
        target_ratio = target_width / max(target_height, 1)
        current = input_video
        try:
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
                speed=speed,
                color_preset=color_preset,
                layout_id=layout_id,
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

        # 5. Verify + GEOMETRY VALIDATION (PART 9, TZ Phase 35): never send
        # a deformed clip. Validation errors MUST NOT fall through the
        # broad except into a "successful" result.
        final_meta = None
        try:
            final_meta = await probe.probe(current)
        except Exception as e:
            logger.warning("quickprep_probe_failed", error=str(e)[:200])
            raise QuickPrepError(f"Final probe failed: {e}") from e
        # Validation WITHOUT broad catch (TZ Phase 35).
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