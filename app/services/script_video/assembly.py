"""Video assembly for AI-generated script videos.

Pipeline:
  1. Images (per scene) -> video stream via concat demuxer or loop
  2. Audio (from TTS) -> audio stream
  3. Subtitles (.ass) burned with drawtext/subtitles filter
  4. FFmpeg output to MP4 (vertical 1080x1920, H.264, AAC)
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Optional

from app.core.config import get_settings
from app.core.logging import get_logger
from app.services.media.ffmpeg import MediaService, _find_ffmpeg_bin
from app.services.script_video.models import VideoPlan, Scene
from app.services.script_video.tts_adapter import ScriptTTSAdapter

logger = get_logger(__name__)


class ScriptVideoAssemblyError(RuntimeError):
    pass


class ScriptVideoAssembler:
    def __init__(self) -> None:
        self.media = MediaService()
        self.settings = get_settings()

    async def assemble(
        self,
        plan: VideoPlan,
        images: list[str],  # URLs or local paths; must be downloaded first
        subtitle_path: Optional[str] = None,
        audio_path: Optional[Path] = None,
        output_path: Optional[Path] = None,
    ) -> Path:
        """Assemble final MP4 from scenes.

        For now, uses a practical FFmpeg approach:
        - Build a concat list of image files (each shown for duration_hint seconds)
        - Mix with audio track
        - Burn subtitles if subtitle_path exists
        - Scale to 1080x1920 vertical

        Returns output_path.
        """
        if not images:
            raise ScriptVideoAssemblyError("No images provided for assembly")

        s = self.settings
        output_path = output_path or Path(tempfile.mkstemp(suffix=".mp4", prefix="script_assembly_")[1])
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Download images if URLs; assume they're local for this stage
        # For simplicity we treat images as paths. In production, download URLs first.
        # Here we assume the caller has downloaded them to a temp directory.
        # We use a concat demuxer approach: list files with durations.

        # Create concat input file for images
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, prefix="concat_") as concat_fh:
            concat_path = concat_fh.name
            # If we have N images and M scenes, we pair them; for simplicity
            # we use first min(len(images), len(scenes)) scenes.
            scenes = list(plan.scenes)
            for i, scene in enumerate(scenes):
                if i >= len(images):
                    break
                img_path = images[i]
                # If image is URL, we'd need to download it; assume path
                # For demo: we write the path as-is assuming it's a file
                # In production, download URLs first.
                # We use loop filter for static duration per image
                duration = max(1.0, float(scene.duration_hint))
                concat_fh.write(f"file '{img_path}\n")
                concat_fh.write(f"duration {duration}\n")
            # Final file must be repeated? Not strictly needed for concat.
            concat_fh.write(f"file '{images[min(len(images)-1, len(scenes)-1)]}\n")

        # Build FFmpeg command
        ffmpeg = _find_ffmpeg_bin()
        cmd = [
            ffmpeg, "-y", "-v", "error",
            "-f", "concat", "-safe", "0", "-i", concat_path,
        ]
        if audio_path and audio_path.exists():
            cmd += ["-i", str(audio_path)]
        else:
            # Generate TTS audio from plan narration
            tts = ScriptTTSAdapter()
            full_narration = " ".join(s.narration for s in plan.scenes)
            audio_path_tmp = output_path.with_suffix(".audio.mp3")
            await tts.synthesize_narration(full_narration, audio_path_tmp)
            cmd += ["-i", str(audio_path_tmp)]

        # Subtitles filter
        subtitle_filter = ""
        if subtitle_path and Path(subtitle_path).exists():
            subtitle_filter = f"subtitles='{subtitle_path}':force_style='FontName=Arial,FontSize=24,PrimaryColour=&HFFFFFF,OutlineColour=&H000000,BackColour=&H80000000,BorderStyle=1,Outline=2,MarginV=60,MarginL=80,MarginR=80'"

        # Scale and format
        cmd += [
            "-vf",
            f"scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(1080-iw)/2:(1920-ih)/2:black,setsar=1/1,{subtitle_filter}" if subtitle_filter else "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(1080-iw)/2:(1920-ih)/2:black,setsar=1/1",
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "23",
            "-r", "30",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "128k",
            "-shortest",
            str(output_path),
        ]

        import asyncio
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.error("assembly_failed", stderr=stderr.decode()[:500], cmd=" ".join(cmd))
            raise ScriptVideoAssemblyError(f"FFmpeg assembly failed: {stderr.decode()[:300]}")

        logger.info("assembly_complete", output_path=str(output_path), scenes=len(scenes))
        return output_path
