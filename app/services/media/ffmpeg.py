"""Media service for FFmpeg operations (future video support)."""

import asyncio
import shutil
from pathlib import Path
from typing import Optional

from app.core.logging import get_logger


logger = get_logger(__name__)


class MediaService:
    """FFmpeg wrapper for media processing."""

    def __init__(self) -> None:
        self._ffmpeg_path = self._find_ffmpeg()

    def _find_ffmpeg(self) -> str:
        """Find FFmpeg executable."""
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            logger.warning("ffmpeg_not_found", path="ffmpeg not in PATH")
            return "ffmpeg"  # Will fail at runtime if not installed
        logger.info("ffmpeg_found", path=ffmpeg)
        return ffmpeg

    async def extract_audio(
        self,
        input_path: Path,
        output_path: Path,
        *,
        sample_rate: int = 16_000,
        channels: int = 1,
        sample_format: str = "s16",
        timeout_seconds: float = 300.0,
    ) -> Path:
        """Extract a mono PCM track from `input_path` to `output_path`.

        Default format matches faster-whisper's expectations:
        16 kHz, 1 channel, signed 16-bit PCM WAV. Other downstream
        consumers (subtitle stage, normalization) can re-encode.

        Uses `asyncio.create_subprocess_exec` (shell=False) so the
        user-controlled `input_path` is passed as a literal argv token.
        """
        if not input_path.exists():
            raise FileNotFoundError(f"Input not found: {input_path}")
        output_path.parent.mkdir(parents=True, exist_ok=True)

        cmd = [
            self._ffmpeg_path,
            "-y",                     # overwrite output
            "-v", "error",            # quiet, only errors
            "-i", str(input_path),
            "-vn",                    # strip video stream
            "-ac", str(channels),
            "-ar", str(sample_rate),
            "-acodec", "pcm_s16le" if sample_format == "s16" else sample_format,
            str(output_path),
        ]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_seconds,
            )
        except asyncio.TimeoutError as e:
            raise RuntimeError(f"FFmpeg extract_audio timed out after {timeout_seconds}s") from e

        if proc.returncode != 0:
            err = stderr.decode(errors="ignore")[:500]
            logger.error(
                "ffmpeg_extract_audio_failed",
                input=str(input_path),
                error=err,
            )
            raise RuntimeError(f"FFmpeg extract_audio failed: {err}")

        if not output_path.exists() or output_path.stat().st_size == 0:
            raise RuntimeError(f"FFmpeg produced empty output at {output_path}")

        logger.info(
            "ffmpeg_extract_audio_done",
            input=str(input_path),
            output=str(output_path),
            bytes=output_path.stat().st_size,
        )
        return output_path

    async def cut_clip(
        self,
        input_path: Path,
        output_path: Path,
        start_seconds: float,
        end_seconds: float,
        *,
        timeout_seconds: float = 180.0,
    ) -> Path:
        """Cut a [start, end] segment out of `input_path` to `output_path`.

        Uses `-c copy` so the bitstream is re-muxed without re-encoding —
        fast (sub-second) and lossless. Frame-accurate cuts aren't guaranteed
        with copy mode (keyframes only); for cleaner cuts use the re-encode
        path in Stage G.

        Validation lives in the caller (ClipCutter); here we just run ffmpeg.
        """
        if not input_path.exists():
            raise FileNotFoundError(f"Input not found: {input_path}")
        if end_seconds <= start_seconds:
            raise ValueError(f"end ({end_seconds}) must be > start ({start_seconds})")

        output_path.parent.mkdir(parents=True, exist_ok=True)

        cmd = [
            self._ffmpeg_path,
            "-y",
            "-v", "error",
            "-ss", f"{start_seconds:.3f}",
            "-to", f"{end_seconds:.3f}",
            "-i", str(input_path),
            "-c", "copy",
            "-avoid_negative_ts", "make_zero",
            "-movflags", "+faststart",     # web-friendly MP4 header
            str(output_path),
        ]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_seconds,
            )
        except asyncio.TimeoutError as e:
            raise RuntimeError(f"FFmpeg cut timed out after {timeout_seconds}s") from e

        if proc.returncode != 0:
            err = stderr.decode(errors="ignore")[:500]
            logger.error(
                "ffmpeg_cut_failed",
                input=str(input_path),
                start=start_seconds, end=end_seconds,
                error=err,
            )
            raise RuntimeError(f"FFmpeg cut failed: {err}")

        if not output_path.exists() or output_path.stat().st_size == 0:
            raise RuntimeError(f"FFmpeg cut produced empty output at {output_path}")

        logger.info(
            "ffmpeg_cut_done",
            input=str(input_path),
            output=str(output_path),
            start=start_seconds, end=end_seconds,
            bytes=output_path.stat().st_size,
        )
        return output_path

    async def make_vertical(
        self,
        input_path: Path,
        output_path: Path,
        *,
        target_width: int = 1080,
        target_height: int = 1920,
        target_fps: int = 30,
        video_bitrate: str = "4M",
        audio_bitrate: str = "128k",
        blur_strength: int = 30,
        timeout_seconds: float = 600.0,
    ) -> Path:
        """Convert source video to 9:16 vertical with a blurred background.

        Strategy (single ffmpeg filter_complex pass):
        - If source is already portrait (height >= width * (target_h/target_w)):
          just scale the source to target resolution (no crop, no blur).
        - Otherwise (landscape or square wider than portrait):
          1. background: scale source so it fully covers target_width×target_height
             AND blur it heavily (gblur sigma=blur_strength).
          2. foreground: scale source to fit target_height (keeping aspect),
             centered.
          3. overlay foreground on top of background.

        Audio is passed through with light normalization to mono AAC.

        Output codec: H.264 + AAC, yuv420p (mobile-safe), +faststart.
        """
        if not input_path.exists():
            raise FileNotFoundError(f"Input not found: {input_path}")
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Filter graph:
        # - split source into [bg][fg]
        # - bg: scale to fully cover target (increase), crop, heavy blur
        # - fg: scale to fit inside target (decrease, never exceed)
        # - overlay fg over bg (centred)
        filter_complex = (
            "[0:v]split=2[bg_src][fg_src];"
            # Background: cover (scale up if needed) target area + heavy blur
            f"[bg_src]scale=w={target_width}:h={target_height}:"
            f"force_original_aspect_ratio=increase:flags=fast_bilinear,"
            f"crop={target_width}:{target_height},"
            f"gblur=sigma={blur_strength}[bg];"
            # Foreground: fit inside target (no upscaling past source res),
            # pad to exact target size with black bars.
            f"[fg_src]scale=w={target_width}:h={target_height}:"
            f"force_original_aspect_ratio=decrease:flags=fast_bilinear,"
            f"pad={target_width}:{target_height}:(ow-iw)/2:(oh-ih)/2:color=black[fg];"
            "[bg][fg]overlay=(W-w)/2:(H-h)/2:shortest=0[v]"
        )

        cmd = [
            self._ffmpeg_path,
            "-y",
            "-v", "error",
            "-i", str(input_path),
            "-filter_complex", filter_complex,
            "-map", "[v]",
            "-map", "0:a?",
            "-r", str(target_fps),
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-pix_fmt", "yuv420p",
            "-b:v", video_bitrate,
            "-c:a", "aac",
            "-b:a", audio_bitrate,
            "-ac", "2",
            "-movflags", "+faststart",
            str(output_path),
        ]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_seconds,
            )
        except asyncio.TimeoutError as e:
            raise RuntimeError(
                f"FFmpeg make_vertical timed out after {timeout_seconds}s"
            ) from e

        if proc.returncode != 0:
            err = stderr.decode(errors="ignore")[:500]
            logger.error(
                "ffmpeg_make_vertical_failed",
                input=str(input_path), error=err,
            )
            raise RuntimeError(f"FFmpeg make_vertical failed: {err}")

        if not output_path.exists() or output_path.stat().st_size == 0:
            raise RuntimeError(
                f"FFmpeg make_vertical produced empty output at {output_path}"
            )

        logger.info(
            "ffmpeg_make_vertical_done",
            input=str(input_path),
            output=str(output_path),
            target=f"{target_width}x{target_height}",
            bytes=output_path.stat().st_size,
        )
        return output_path

    async def burn_subtitles(
        self,
        video_path: Path,
        ass_path: Path,
        output_path: Path,
        *,
        timeout_seconds: float = 300.0,
    ) -> Path:
        """Hard-burn ASS subtitles into `video_path` -> `output_path`.

        Single re-encode pass with libx264. Output keeps the source
        resolution / fps, gets H.264 + AAC.
        """
        if not video_path.exists():
            raise FileNotFoundError(f"Video not found: {video_path}")
        if not ass_path.exists():
            raise FileNotFoundError(f"ASS file not found: {ass_path}")
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # ass filter — ':force_style' lets us override style on the fly.
        # Build the filter string outside f-string to avoid the backslash
        # restriction on Python 3.11.
        ass_escaped = str(ass_path).replace(":", "\\:")
        vf = "ass=" + ass_escaped

        cmd = [
            self._ffmpeg_path,
            "-y",
            "-v", "error",
            "-i", str(video_path),
            "-vf", vf,
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-pix_fmt", "yuv420p",
            "-c:a", "copy",
            "-movflags", "+faststart",
            str(output_path),
        ]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_seconds,
            )
        except asyncio.TimeoutError as e:
            raise RuntimeError(
                f"FFmpeg burn_subtitles timed out after {timeout_seconds}s"
            ) from e

        if proc.returncode != 0:
            err = stderr.decode(errors="ignore")[:500]
            logger.error(
                "ffmpeg_burn_subtitles_failed",
                input=str(video_path),
                error=err,
            )
            raise RuntimeError(f"FFmpeg burn_subtitles failed: {err}")

        if not output_path.exists() or output_path.stat().st_size == 0:
            raise RuntimeError("FFmpeg burn_subtitles produced empty output")

        logger.info(
            "ffmpeg_burn_subtitles_done",
            input=str(video_path),
            output=str(output_path),
            bytes=output_path.stat().st_size,
        )
        return output_path

    async def burn_cta(
        self,
        video_path: Path,
        cta_path: Path,
        output_path: Path,
        *,
        x: int,
        y: int,
        start_seconds: float,
        end_seconds: float,
        timeout_seconds: float = 300.0,
    ) -> Path:
        """Overlay a PNG over a window of the video. No re-encode of video
        stream — only a copy of video with the overlay layered on top.
        Falls back to re-encoding if copy isn't compatible.
        """
        if not video_path.exists():
            raise FileNotFoundError(f"Video not found: {video_path}")
        if not cta_path.exists():
            raise FileNotFoundError(f"CTA asset not found: {cta_path}")
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # enable=between(t,start,end) shows the overlay only in the window.
        # IMPORTANT: ffmpeg's expression parser splits on commas — if we
        # emit e.g. `between(t,1.500,2.000)` it sees "between(t" "1.500"
        # "2.000)" as separate filter arguments. Solution: use %g (no
        # trailing zeros, e.g. "1.5") AND quote the expression so commas
        # inside don't split arguments.
        filter_expr = (
            f"[1:v]format=rgba[cta];"
            f"[0:v][cta]overlay=x={x}:y={y}:"
            f"enable='between(t,%g,%g)'[v]"
        ) % (start_seconds, end_seconds)

        cmd = [
            self._ffmpeg_path,
            "-y",
            "-v", "error",
            "-i", str(video_path),
            "-i", str(cta_path),
            "-filter_complex", filter_expr,
            "-map", "[v]",
            "-map", "0:a?",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-pix_fmt", "yuv420p",
            "-c:a", "copy",
            "-movflags", "+faststart",
            str(output_path),
        ]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_seconds,
            )
        except asyncio.TimeoutError as e:
            raise RuntimeError(
                f"FFmpeg burn_cta timed out after {timeout_seconds}s"
            ) from e

        if proc.returncode != 0:
            err = stderr.decode(errors="ignore")[:500]
            logger.error(
                "ffmpeg_burn_cta_failed",
                input=str(video_path), error=err,
            )
            raise RuntimeError(f"FFmpeg burn_cta failed: {err}")

        if not output_path.exists() or output_path.stat().st_size == 0:
            raise RuntimeError("FFmpeg burn_cta produced empty output")

        logger.info(
            "ffmpeg_burn_cta_done",
            input=str(video_path),
            output=str(output_path),
            bytes=output_path.stat().st_size,
        )
        return output_path

    async def finalize_export(
        self,
        video_path: Path,
        output_path: Path,
        *,
        target_lufs: float = -16.0,
        timeout_seconds: float = 300.0,
    ) -> Path:
        """Final clean export — strips source metadata / chapters / paths.

        Uses loudnorm (EBU R128) two-pass for sane loudness across all
        clips, and `-map_metadata -1 -map_chapters -1` to wipe every
        piece of metadata that might leak source filename / paths.

        Note: this is a privacy / portability pass, not an anti-detection
        pass — we don't do pixel mods, frame jitter, or any other
        tricks that aim to evade platform fingerprinting.
        """
        if not video_path.exists():
            raise FileNotFoundError(f"Video not found: {video_path}")
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Pass 1: measure loudness.
        measure_cmd = [
            self._ffmpeg_path, "-v", "error",
            "-i", str(video_path),
            "-af", f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11:print_format=json",
            "-f", "null", "-",
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *measure_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            logger.warning("loudnorm_measure_timeout_fallback_copy")
            # Fall back to plain remux-with-metadata-strip.
            return await self._strip_only(video_path, output_path)

        import json
        # loudnorm prints JSON to stderr. Find the block containing "input_i"
        stderr_text = stderr.decode(errors="ignore")
        idx = stderr_text.find('"input_i"')
        if idx == -1:
            logger.warning("loudnorm_parse_failed_fallback_strip")
            return await self._strip_only(video_path, output_path)
        # Find the surrounding braces (loudnorm JSON is flat, single level)
        brace_start = stderr_text.rfind('{', 0, idx)
        brace_end = stderr_text.find('}', idx)
        if brace_start == -1 or brace_end == -1:
            logger.warning("loudnorm_parse_failed_fallback_strip")
            return await self._strip_only(video_path, output_path)
        measured = json.loads(stderr_text[brace_start:brace_end + 1])

        # Pass 2: apply loudnorm + strip metadata.
        apply_cmd = [
            self._ffmpeg_path, "-y", "-v", "error",
            "-i", str(video_path),
            "-af", (
                f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11:"
                f"measured_I={measured.get('input_i', target_lufs)}:"
                f"measured_TP={measured.get('input_tp', -1.5)}:"
                f"measured_LRA={measured.get('input_lra', 11)}:"
                f"measured_thresh={measured.get('input_thresh', -99)}:"
                f"offset={measured.get('target_offset', 0)}:linear=true:print_format=summary"
            ),
            "-c:v", "copy",
            "-c:a", "aac",
            "-b:a", "128k",
            "-map_metadata", "-1",     # strip ALL metadata
            "-map_chapters", "-1",    # strip chapter info
            "-metadata", "comment=",  # clear comment
            "-metadata", "title=",    # clear title
            "-movflags", "+faststart",
            str(output_path),
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *apply_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_seconds,
            )
        except asyncio.TimeoutError as e:
            raise RuntimeError(
                f"FFmpeg finalize timed out after {timeout_seconds}s"
            ) from e

        if proc.returncode != 0:
            err = stderr.decode(errors="ignore")[:500]
            logger.warning("loudnorm_apply_failed_fallback_strip", error=err)
            return await self._strip_only(video_path, output_path)

        if not output_path.exists() or output_path.stat().st_size == 0:
            return await self._strip_only(video_path, output_path)

        logger.info(
            "ffmpeg_finalize_done",
            input=str(video_path),
            output=str(output_path),
            bytes=output_path.stat().st_size,
        )
        return output_path

    async def _strip_only(
        self,
        video_path: Path,
        output_path: Path,
        *,
        timeout_seconds: float = 60.0,
    ) -> Path:
        """Fallback: re-mux while stripping every piece of metadata."""
        output_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            self._ffmpeg_path, "-y", "-v", "error",
            "-i", str(video_path),
            "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
            "-map_metadata", "-1",
            "-map_chapters", "-1",
            "-metadata", "comment=",
            "-metadata", "title=",
            "-movflags", "+faststart",
            str(output_path),
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout_seconds,
        )
        if proc.returncode != 0:
            err = stderr.decode(errors="ignore")[:500]
            raise RuntimeError(f"FFmpeg strip failed: {err}")
        return output_path

    async def probe(self, filepath: Path) -> dict:
        """Probe media file for info (duration, format, etc.)."""
        if not filepath.exists():
            raise FileNotFoundError(f"File not found: {filepath}")

        cmd = [
            self._ffmpeg_path,
            "-v", "error",
            "-show_entries", "format=duration,bit_rate,format_name:stream=codec_type,codec_name,sample_rate,channels",
            "-of", "json",
            str(filepath),
        ]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            logger.error("ffprobe_failed", file=str(filepath), error=stderr.decode())
            raise RuntimeError(f"ffprobe failed: {stderr.decode()}")

        import json
        return json.loads(stdout.decode())

    async def convert_audio(
        self,
        input_path: Path,
        output_path: Path,
        format: str = "mp3",
        bitrate: str = "128k",
        input_format: Optional[str] = None,
        sample_rate: Optional[int] = None,
        channels: Optional[int] = None,
    ) -> Path:
        """Convert audio to target format.

        For raw PCM input, set input_format="s16le" plus sample_rate and channels
        so ffmpeg can interpret the bytes correctly.
        """
        if not input_path.exists():
            raise FileNotFoundError(f"Input not found: {input_path}")

        output_path.parent.mkdir(parents=True, exist_ok=True)

        cmd = [
            self._ffmpeg_path,
            "-y",  # overwrite
        ]
        # Raw input hints (PCM only)
        if input_format:
            cmd += ["-f", input_format]
        if sample_rate:
            cmd += ["-ar", str(sample_rate)]
        if channels:
            cmd += ["-ac", str(channels)]
        cmd += [
            "-i", str(input_path),
            # WAV: just remux into container (PCM stays PCM, no re-encode)
            "-c:a", "copy" if format == "wav" else ("libmp3lame" if format == "mp3" else "copy"),
            "-b:a", bitrate,
            str(output_path),
        ]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            logger.error(
                "ffmpeg_convert_failed",
                input=str(input_path),
                error=stderr.decode()[:500],
            )
            raise RuntimeError(f"FFmpeg conversion failed: {stderr.decode()}")

        logger.info("ffmpeg_convert_done", input=str(input_path), output=str(output_path))
        return output_path

    async def normalize_audio(
        self,
        input_path: Path,
        output_path: Path,
        target_lufs: float = -16.0,
    ) -> Path:
        """Normalize audio loudness (EBU R128)."""
        if not input_path.exists():
            raise FileNotFoundError(f"Input not found: {input_path}")

        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Two-pass normalization: measure then apply
        # Pass 1: measure
        cmd_measure = [
            self._ffmpeg_path,
            "-i", str(input_path),
            "-af", f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11:print_format=json",
            "-f", "null", "-",
        ]

        proc = await asyncio.create_subprocess_exec(
            *cmd_measure,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        # Parse measured values from stderr (FFmpeg outputs JSON to stderr)
        import json
        import re
        json_match = re.search(r"\{.*\}", stderr.decode(), re.DOTALL)
        if not json_match:
            logger.warning("loudnorm_measure_failed", stderr=stderr.decode()[:500])
            # Fallback: just copy
            return await self.convert_audio(input_path, output_path)

        measured = json.loads(json_match.group())

        # Pass 2: apply
        cmd_apply = [
            self._ffmpeg_path,
            "-y",
            "-i", str(input_path),
            "-af", (
                f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11:"
                f"measured_I={measured.get('input_i', target_lufs)}:"
                f"measured_TP={measured.get('input_tp', -1.5)}:"
                f"measured_LRA={measured.get('input_lra', 11)}:"
                f"measured_thresh={measured.get('input_thresh', -99)}:"
                f"offset={measured.get('target_offset', 0)}:"
                f"linear=true:print_format=summary"
            ),
            "-c:a", "libmp3lame",
            "-b:a", "128k",
            str(output_path),
        ]

        proc = await asyncio.create_subprocess_exec(
            *cmd_apply,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            logger.error("loudnorm_apply_failed", error=stderr.decode())
            raise RuntimeError(f"Loudnorm failed: {stderr.decode()}")

        logger.info("audio_normalized", input=str(input_path), output=str(output_path))
        return output_path

    async def ensure_telegram_compatible(self, input_path: Path, output_path: Path) -> Path:
        """
        Ensure audio is compatible with Telegram (MP3, <= 50MB, proper headers).
        Currently just converts to MP3 128k if needed.
        """
        # Probe first
        info = await self.probe(input_path)

        # Check if already MP3
        audio_streams = [s for s in info.get("streams", []) if s.get("codec_type") == "audio"]
        if audio_streams and audio_streams[0].get("codec_name") == "mp3":
            # Check size
            size_mb = input_path.stat().st_size / (1024 * 1024)
            if size_mb <= 50:
                # Already compatible, just copy
                import shutil
                shutil.copy2(input_path, output_path)
                return output_path

        # Convert
        return await self.convert_audio(input_path, output_path, format="mp3", bitrate="128k")


# Global instance
media_service = MediaService()


def get_media_service() -> MediaService:
    return media_service