"""Media service — audio/video processing via FFmpeg.

`MediaService` exposes audio-only helpers used elsewhere.
`FFprobeService` (in `probe.py`) handles metadata extraction.
"""
from app.services.media.ffmpeg import MediaService, get_media_service
from app.services.media.probe import (
    FFprobeService,
    ProbeError,
    VideoProbeResult,
    get_probe_service,
)

__all__ = [
    "MediaService",
    "get_media_service",
    "FFprobeService",
    "ProbeError",
    "VideoProbeResult",
    "get_probe_service",
]
