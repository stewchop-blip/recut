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