"""OutputCompressor (Phase 8, audit #40-41) — adapted from Downy compression
fallback + ffmpeg-video-bot compress_video().

compress_to_target(): if output > max_bytes → duration probe → reserve audio
bitrate → compute video bitrate → encode H264/AAC → check → second pass with
lower bitrate (and optional resolution reduction). One config for the
Telegram limit (audit #41).
"""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from app.core.logging import get_logger

logger = get_logger(__name__)

# ONE config for the sender limit (audit #41)
TELEGRAM_SEND_LIMIT_BYTES = 49 * 1024 * 1024
SAFE_TARGET_BYTES = int(TELEGRAM_SEND_LIMIT_BYTES * 0.95)
MIN_BITRATE_KBPS = 500


class CompressionError(RuntimeError):
    error_code = "COMPRESSION_FAILED"


class OutputCompressor:
    def __init__(self) -> None:
        self._ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
        self._ffprobe = shutil.which("ffprobe") or "ffprobe"

    async def _probe(self, path: Path) -> tuple[float, int, int]:
        proc = await asyncio.create_subprocess_exec(
            self._ffprobe, "-v", "error", "-print_format", "json",
            "-show_streams", "-show_format", str(path),
            stdout=asyncio.subprocess.PIPE)
        out, _ = await proc.communicate()
        import json
        d = json.loads(out.decode())
        v = next((s for s in d.get("streams", []) if s.get("codec_type") == "video"), {})
        dur = float(d.get("format", {}).get("duration") or 0)
        return (dur, int(v.get("width") or 0), int(v.get("height") or 0))

    async def compress_to_target(self, input_path: Path, output_path: Path,
                                 max_bytes: int = SAFE_TARGET_BYTES,
                                 min_resolution: int = 480) -> Path:
        """Ensure output <= max_bytes; returns compressed path (same if fits)."""
        size = input_path.stat().st_size
        if size <= max_bytes:
            return input_path
        dur, w, h = await self._probe(input_path)
        if dur <= 0:
            raise CompressionError("no duration")
        for attempt, reserve_kbps in ((0, 128), (1, 96)):
            target_bits = max_bytes * 8 * 0.93            # container overhead
            total_kbps = int(target_bits / max(dur, 0.1) / 1000)
            video_kbps = max(MIN_BITRATE_KBPS, total_kbps - reserve_kbps)
            cur_w, cur_h = w, h
            if attempt == 1 and cur_h > min_resolution * 2:
                # resolution reduction on the second pass
                cur_h = max(min_resolution * 2, (cur_h // 2) // 2 * 2)
                cur_w = int(cur_w * cur_h / max(h, 1)) // 2 * 2
            vf = f"scale={cur_w}:{cur_h}" if (cur_w, cur_h) != (w, h) else "anull"
            cmd = [self._ffmpeg, "-y", "-v", "error", "-i", str(input_path),
                   "-vf", vf,
                   "-c:v", "libx264", "-preset", "medium", "-b:v", f"{video_kbps}k",
                   "-maxrate", f"{int(video_kbps * 1.4)}k",
                   "-bufsize", f"{video_kbps * 2}k",
                   "-c:a", "aac", "-b:a", f"{reserve_kbps}k",
                   "-movflags", "+faststart", str(output_path)]
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                _, err = await asyncio.wait_for(proc.communicate(), 1800)
            except asyncio.TimeoutError as e:
                proc.kill()
                raise CompressionError("compress timed out") from e
            if proc.returncode != 0:
                raise CompressionError(
                    f"compress failed: {err.decode(errors='ignore')[:200]}")
            out_size = output_path.stat().st_size
            logger.info("output_compressed",
                        input=str(input_path), in_bytes=size, out_bytes=out_size,
                        video_kbps=video_kbps, attempt=attempt, res=f"{cur_w}x{cur_h}")
            if out_size <= max_bytes:
                return output_path
        raise CompressionError("still above target after second pass")


_compressor: OutputCompressor | None = None


def get_output_compressor() -> OutputCompressor:
    global _compressor
    if _compressor is None:
        _compressor = OutputCompressor()
    return _compressor