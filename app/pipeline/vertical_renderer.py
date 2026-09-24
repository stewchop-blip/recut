"""VerticalRenderer — converts cut clips into 9:16 MP4s.

Stage G of the pipeline. For each clip produced by Stage F:

1. Probe the input clip to know its width/height.
2. Render at 1080x1920 (configurable) using a single FFmpeg filter
   graph that scales + blurs the background and overlays the original
   on top — preserving all content (no cropping).

Result: vertical_01.mp4, vertical_02.mp4, ... ready for Stage H
(subtitles).
"""
from dataclasses import dataclass
from pathlib import Path

from app.core.config import get_settings
from app.core.logging import get_logger
from app.pipeline.clip_cutter import CutJob
from app.services.media import get_media_service, get_probe_service

logger = get_logger(__name__)


class VerticalRenderError(RuntimeError):
    """Stage G failed."""


@dataclass(slots=True, frozen=True)
class VerticalClip:
    """One vertically-rendered clip."""
    index: int
    cut_path: Path
    output_path: Path
    width: int
    height: int
    size_bytes: int


@dataclass(slots=True, frozen=True)
class VerticalJob:
    clips: tuple[VerticalClip, ...]


class VerticalRenderer:
    """Stateless service."""

    def __init__(
        self,
        target_width: int | None = None,
        target_height: int | None = None,
        target_fps: int | None = None,
        video_bitrate: str | None = None,
        audio_bitrate: str | None = None,
        blur_strength: int = 30,
        *,
        background_id: str = "blur",
        title_text: str = "",
        brand_corner: bool = False,
    ) -> None:
        s = get_settings()
        self._w = target_width or s.output_width
        self._h = target_height or s.output_height
        self._fps = target_fps or s.output_fps
        self._vbr = video_bitrate or s.output_video_bitrate
        self._abr = audio_bitrate or s.output_audio_bitrate
        self._blur = blur_strength
        # PHASE F: user style travels with the renderer (same engine as
        # QuickPrep — one layout, one aspect implementation everywhere).
        self._background_id = background_id
        self._title_text = title_text
        self._brand_corner = brand_corner

    async def render(self, cut_job: CutJob, output_dir: Path) -> VerticalJob:
        output_dir.mkdir(parents=True, exist_ok=True)
        media = get_media_service()
        probe = get_probe_service()
        out: list[VerticalClip] = []

        for cut in cut_job.clips:
            try:
                meta = await probe.probe(cut.output_path)
            except Exception as e:
                logger.warning(
                    "vertical_probe_failed",
                    clip_index=cut.index,
                    error=str(e)[:200],
                )
                continue

            out_path = output_dir / f"vertical_{cut.index:02d}.mp4"
            try:
                await media.make_vertical(
                    input_path=cut.output_path,
                    output_path=out_path,
                    target_width=self._w,
                    target_height=self._h,
                    target_fps=self._fps,
                    video_bitrate=self._vbr,
                    audio_bitrate=self._abr,
                    blur_strength=self._blur,
                    background_id=self._background_id,
                    title_text=self._title_text,
                    brand_corner=self._brand_corner,
                )
            except (RuntimeError, FileNotFoundError) as e:
                logger.error(
                    "vertical_render_failed",
                    clip_index=cut.index,
                    error=str(e)[:200],
                )
                continue

            out.append(VerticalClip(
                index=cut.index,
                cut_path=cut.output_path,
                output_path=out_path,
                width=self._w,
                height=self._h,
                size_bytes=out_path.stat().st_size,
            ))

        if not out:
            raise VerticalRenderError("No clips could be rendered vertical")

        logger.info(
            "vertical_clips_ready",
            count=len(out),
            target=f"{self._w}x{self._h}",
        )
        return VerticalJob(clips=tuple(out))