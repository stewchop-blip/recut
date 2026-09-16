"""ffprobe wrapper — validates the file and returns structured metadata.

Uses `asyncio.create_subprocess_exec` (NOT shell=True) so user-controlled
file paths are safe — they're passed as argv tokens, never interpolated
into a command string.
"""
import asyncio
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from app.core.logging import get_logger

logger = get_logger(__name__)


class ProbeError(RuntimeError):
    """ffprobe failed or returned an unexpected shape."""


@dataclass(slots=True, frozen=True)
class VideoProbeResult:
    """Structured metadata for one media file."""
    duration_seconds: float
    width: int
    height: int
    fps: float
    has_audio: bool
    video_codec: str
    audio_codec: str | None
    rotation: int        # 0/90/180/270 — applied rotation in degrees
    bitrate_kbps: int


_FPS_RE = re.compile(r"(\d+)\s*/\s*(\d+)")


def _parse_fps(rate: str | None) -> float:
    if not rate:
        return 0.0
    m = _FPS_RE.match(rate)
    if not m:
        try:
            return float(rate)
        except ValueError:
            return 0.0
    num, den = int(m.group(1)), int(m.group(2))
    return num / den if den else 0.0


def _parse_rotation(tags: dict) -> int:
    """Returns rotation in degrees (0/90/180/270).

    Some sources put rotation in side_data_list as displaymatrix; we
    intentionally keep this simple — most Telegram uploads are already
    pre-rotated and have no rotation tag.
    """
    val = tags.get("rotate") if isinstance(tags, dict) else None
    if val is None:
        return 0
    try:
        rot = int(float(val))
    except (ValueError, TypeError):
        return 0
    rot = rot % 360
    return rot if rot in (0, 90, 180, 270) else 0


class FFprobeService:
    """Thin wrapper around `ffprobe -v error -of json -show_streams ...`."""

    def __init__(self) -> None:
        self._ffprobe = shutil.which("ffprobe") or "ffprobe"
        if self._ffprobe == "ffprobe" and not shutil.which("ffprobe"):
            logger.warning("ffprobe_not_found")

    async def probe(self, file_path: Path) -> VideoProbeResult:
        """Return parsed metadata, or raise ProbeError."""
        if not file_path.exists():
            raise ProbeError(f"File not found: {file_path}")

        cmd = [
            self._ffprobe,
            "-v", "error",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            str(file_path),
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            err = stderr.decode(errors="ignore")[:500]
            logger.error("ffprobe_failed", file=str(file_path), error=err)
            raise ProbeError(f"ffprobe failed: {err}")

        try:
            data = json.loads(stdout.decode(errors="ignore"))
        except json.JSONDecodeError as e:
            raise ProbeError(f"ffprobe returned non-JSON: {e}") from e

        return self._parse(data)

    @staticmethod
    def _parse(data: dict) -> VideoProbeResult:
        streams = data.get("streams") or []
        fmt = data.get("format") or {}

        video = next((s for s in streams if s.get("codec_type") == "video"), None)
        if video is None:
            raise ProbeError("No video stream found")

        audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

        # Duration: prefer format.duration (most accurate), fall back to video stream.
        duration = 0.0
        try:
            duration = float(fmt.get("duration") or video.get("duration") or 0)
        except (ValueError, TypeError):
            duration = 0.0

        width = int(video.get("width") or 0)
        height = int(video.get("height") or 0)
        fps = _parse_fps(video.get("avg_frame_rate") or video.get("r_frame_rate"))

        # Bitrate (kbps): try format.bit_rate first.
        bitrate_kbps = 0
        try:
            br = fmt.get("bit_rate") or video.get("bit_rate")
            if br:
                bitrate_kbps = int(int(br) // 1000)
        except (ValueError, TypeError):
            bitrate_kbps = 0

        rotation = _parse_rotation(video.get("tags") or {})

        return VideoProbeResult(
            duration_seconds=duration,
            width=width,
            height=height,
            fps=fps,
            has_audio=audio is not None,
            video_codec=str(video.get("codec_name") or ""),
            audio_codec=str(audio.get("codec_name")) if audio else None,
            rotation=rotation,
            bitrate_kbps=bitrate_kbps,
        )


# Global instance (ffprobe path doesn't change at runtime)
probe_service = FFprobeService()


def get_probe_service() -> FFprobeService:
    return probe_service
