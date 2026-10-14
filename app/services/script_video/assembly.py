"""Assemble generated stills, narration, and ASS subtitles into vertical MP4."""
from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Optional

from app.core.logging import get_logger
from app.services.media.ffmpeg import _find_ffmpeg_bin
from app.services.script_video.models import VideoPlan

logger = get_logger(__name__)


class ScriptVideoAssemblyError(RuntimeError):
    pass


class ScriptVideoAssembler:
    async def assemble(
        self,
        plan: VideoPlan,
        images: list[str],
        subtitle_path: Optional[str] = None,
        audio_path: Optional[Path] = None,
        output_path: Optional[Path] = None,
    ) -> Path:
        if not images:
            raise ScriptVideoAssemblyError("No images provided for assembly")
        image_paths = [Path(p).resolve() for p in images]
        if any(not p.is_file() for p in image_paths):
            raise ScriptVideoAssemblyError("An image file is missing")
        if audio_path is None or not Path(audio_path).is_file():
            raise ScriptVideoAssemblyError("Narration audio is missing")

        output_path = Path(output_path) if output_path else Path("/tmp") / f"script_{uuid.uuid4().hex}.mp4"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        duration_each = max(1.0, float(plan.target_duration) / len(image_paths))
        concat_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", suffix=".txt", delete=False, prefix="recut_concat_", encoding="utf-8"
            ) as concat:
                concat_path = concat.name
                for image in image_paths:
                    # Temporary directories use a controlled, quote-free prefix.
                    if "'" in str(image) or "\n" in str(image):
                        raise ScriptVideoAssemblyError("Unsafe image path")
                    concat.write(f"file '{image}'\n")
                    concat.write(f"duration {duration_each:.3f}\n")
                concat.write(f"file '{image_paths[-1]}'\n")

            filters = [
                "scale=1080:1920:force_original_aspect_ratio=decrease",
                "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black",
                "setsar=1",
            ]
            if subtitle_path and Path(subtitle_path).is_file():
                escaped = str(Path(subtitle_path).resolve()).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
                filters.append(
                    f"subtitles='{escaped}':force_style='FontName=Arial,FontSize=24,"
                    "PrimaryColour=&H00FFFFFF,OutlineColour=&H000000,BorderStyle=1,Outline=2,MarginV=60'"
                )
            command = [
                _find_ffmpeg_bin(), "-y", "-v", "error", "-f", "concat", "-safe", "0",
                "-i", concat_path, "-i", str(Path(audio_path).resolve()),
                "-vf", ",".join(filters), "-c:v", "libx264", "-preset", "fast",
                "-crf", "23", "-r", "30", "-pix_fmt", "yuv420p", "-c:a", "aac",
                "-b:a", "128k", "-shortest", "-movflags", "+faststart", str(output_path),
            ]
            proc = await asyncio.create_subprocess_exec(
                *command, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                raise ScriptVideoAssemblyError("FFmpeg timed out")
            if proc.returncode:
                logger.error("script_video_ffmpeg_failed", exit_code=proc.returncode,
                             stderr=(stderr or b"").decode(errors="replace")[-1000:])
                raise ScriptVideoAssemblyError("FFmpeg failed to assemble video")
            if not output_path.is_file() or output_path.stat().st_size == 0:
                raise ScriptVideoAssemblyError("FFmpeg produced an empty video")
            logger.info("script_video_assembly_complete", image_count=len(image_paths),
                        output_bytes=output_path.stat().st_size)
            return output_path
        finally:
            if concat_path:
                Path(concat_path).unlink(missing_ok=True)
