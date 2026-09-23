"""Final renderer — Stages H (subs) + I (CTA) + J (clean export) in one go.

Per clip:
  vertical_NN.mp4 (Stage G output)
    -> subs_NN.mp4    (with subtitles burned)
       -> cta_NN.mp4 (with CTA overlaid, if enabled)
          -> final_NN.mp4 (clean export, loudnorm, no source metadata)

Each step falls back gracefully on failure: a failed step keeps the
previous file as the input for the next one, so we always return
*something* playable for every clip.
"""
from dataclasses import dataclass
from pathlib import Path

from app.core.config import get_settings
from app.core.logging import get_logger
from app.pipeline.transcriber import TranscribedJob
from app.services.media import get_media_service
from app.services.overlays import CTAService
from app.services.subtitles import AssSubtitleBuilder

logger = get_logger(__name__)


class FinalRenderError(RuntimeError):
    """Stage H+I+J failed completely (no clip output at all)."""


@dataclass(slots=True, frozen=True)
class FinalClip:
    index: int
    final_path: Path
    has_subtitles: bool
    has_cta: bool
    size_bytes: int


@dataclass(slots=True, frozen=True)
class FinalJob:
    clips: tuple[FinalClip, ...]


class FinalRenderer:
    """Stage H + I + J combined orchestrator."""

    def __init__(self) -> None:
        s = get_settings()
        self._max_words = s.subtitle_max_words_per_line

    async def render(
        self,
        vertical_clips: list,           # list of VerticalClip from Stage G
        transcribed: TranscribedJob,    # from Stage D
        output_dir: Path,
        cta_configured_asset: str,
    ) -> FinalJob:
        output_dir.mkdir(parents=True, exist_ok=True)
        media = get_media_service()
        cta_service = CTAService()
        subtitle_builder = AssSubtitleBuilder()

        out: list[FinalClip] = []
        for vc in vertical_clips:
            clip_start = getattr(vc, "source_start", 0.0)
            clip_end = getattr(vc, "source_end", 0.0)
            vertical_path: Path = vc.output_path
            index = vc.index

            try:
                result = await self._process_one(
                    vertical_path=vertical_path,
                    transcribed=transcribed,
                    clip_start=clip_start,
                    clip_end=clip_end,
                    output_dir=output_dir,
                    index=index,
                    cta_service=cta_service,
                    subtitle_builder=subtitle_builder,
                    cta_configured_asset=cta_configured_asset,
                    media=media,
                )
                final_path, has_subs, has_cta = result
                out.append(FinalClip(
                    index=index,
                    final_path=final_path,
                    has_subtitles=has_subs,
                    has_cta=has_cta,
                    size_bytes=final_path.stat().st_size if final_path.exists() else 0,
                ))
            except Exception as e:
                logger.error(
                    "final_render_clip_failed",
                    index=index, error=str(e)[:200],
                )
                if vertical_path.exists():
                    out.append(FinalClip(
                        index=index,
                        final_path=vertical_path,
                        has_subtitles=False, has_cta=False,
                        size_bytes=vertical_path.stat().st_size,
                    ))

        if not out:
            raise FinalRenderError("No clips could be finalized")

        logger.info("final_render_done", count=len(out))
        return FinalJob(clips=tuple(out))

    async def _process_one(
        self,
        vertical_path: Path,
        transcribed: TranscribedJob,
        clip_start: float,
        clip_end: float,
        output_dir: Path,
        index: int,
        cta_service: CTAService,
        subtitle_builder: AssSubtitleBuilder,
        cta_configured_asset: str,
        media,
    ) -> tuple[Path, bool, bool]:
        current = vertical_path
        has_subs = False
        has_cta = False

        # 1. Build ASS subtitle file.
        from app.services.subtitles.base import (
            SubtitleBuildRequest, TranscriptSegment,
        )
        ass_path: Path | None = None
        try:
            ass_req = SubtitleBuildRequest(
                segments=tuple(
                    TranscriptSegment(start=s.start, end=s.end, text=s.text)
                    for s in transcribed.segments
                ),
                clip_start=clip_start,
                clip_end=clip_end,
                max_words_per_line=self._max_words,
            )
            ass_result = subtitle_builder.build(ass_req)
            if ass_result.output_path:
                ass_path = Path(ass_result.output_path)
        except Exception as e:
            logger.warning("subtitle_build_failed", index=index, error=str(e)[:200])

        # 2. Burn subtitles (if we have an ASS file).
        if ass_path is not None and ass_path.exists():
            subs_path = output_dir / f"subs_{index:02d}.mp4"
            try:
                await media.burn_subtitles(current, ass_path, subs_path)
                current = subs_path
                has_subs = True
            except Exception as e:
                logger.warning("burn_subtitles_failed", index=index, error=str(e)[:200])

        # 3. CTA overlay (if enabled).
        clip_duration = max(0.1, clip_end - clip_start)
        cta_spec = cta_service.resolve(
            clip_duration=clip_duration,
            configured_asset=cta_configured_asset,
            fallback_dir=output_dir,
        )
        if cta_spec is not None:
            cta_path = output_dir / f"cta_{index:02d}.mp4"
            try:
                await media.burn_cta(
                    current, cta_spec.asset_path, cta_path,
                    position=cta_service._position,
                    margin=cta_service._margin,
                    start_seconds=cta_spec.start_seconds,
                    end_seconds=cta_spec.end_seconds,
                )
                current = cta_path
                has_cta = True
            except Exception as e:
                logger.warning("burn_cta_failed", index=index, error=str(e)[:200])

        # 4. Final clean export (loudnorm + strip metadata).
        final_path = output_dir / f"final_{index:02d}.mp4"
        try:
            await media.finalize_export(current, final_path)
            return final_path, has_subs, has_cta
        except Exception as e:
            logger.warning("finalize_export_failed", index=index, error=str(e)[:200])
            return (current if current.exists() else vertical_path), has_subs, has_cta