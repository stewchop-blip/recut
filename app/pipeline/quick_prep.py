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

        # 2. Vertical format (pass-through if already 9:16).
        current = input_video
        is_portrait = meta.height >= meta.width  # loose check
        try:
            if is_portrait and abs(meta.width / max(meta.height, 1) - target_width / target_height) < 0.05:
                # Already 9:16 (within 5%) — copy as-is, but normalize SAR if needed.
                vertical_path = job_dir / "vertical.mp4"
                import shutil
                shutil.copy2(current, vertical_path)
                logger.info("quickprep_passthrough_vertical", path=str(vertical_path))
                # If SAR not square, remux with setsar=1 via a quick ffmpeg copy pass.
                if abs(meta.sample_aspect_ratio - 1.0) > 0.01:
                    sar_fixed = job_dir / "vertical_sar_fixed.mp4"
                    await get_media_service()._run_sar_fix(vertical_path, sar_fixed)
                    vertical_path = sar_fixed
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
                )
            current = vertical_path
        except Exception as e:
            logger.warning("quickprep_vertical_failed_using_source", error=str(e)[:200])
            current = input_video  # fall back to source

        # 3. CTA overlay.
        has_cta = False
        # cta_asset may be:
        # - a user-uploaded PNG path (from DB) — pass through
        # - empty/None but CTA_ENABLED=true — fall back to ensure_cta_asset
        #   which generates a default "Recut" PNG on the fly
        effective_cta_asset: Path | None = None
        if cta_asset is not None and cta_asset.exists():
            effective_cta_asset = cta_asset
        elif cta_asset is not None:
            # DB has a path but file is gone — generate a default in the
            # job_dir so the user still gets an overlay.
            from app.services.overlays.cta_generator import ensure_cta_asset
            effective_cta_asset, _ = ensure_cta_asset("", job_dir)
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

        # 5. Verify.
        try:
            final_meta = await probe.probe(current)
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