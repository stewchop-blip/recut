"""Audio extraction — strip audio track from the source video.

Stage C of the pipeline. After probe confirms the video has an audio
stream we extract a mono 16 kHz 16-bit PCM WAV suitable for
faster-whisper.

Stages downstream:
- D: faster-whisper transcription on the WAV
- (later) loudness measurement before rendering
"""
from dataclasses import dataclass
from pathlib import Path

from app.core.logging import get_logger
from app.services.media import get_media_service

logger = get_logger(__name__)


class AudioExtractionError(RuntimeError):
    """Raised when audio extraction fails or input has no audio."""


@dataclass(slots=True, frozen=True)
class ExtractedAudio:
    path: Path
    duration_seconds: float
    sample_rate: int
    channels: int


class AudioExtractor:
    """Stateless service: extract_audio(video) -> ExtractedAudio."""

    # Defaults match faster-whisper's optimal input.
    DEFAULT_SAMPLE_RATE = 16_000
    DEFAULT_CHANNELS = 1

    def __init__(
        self,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        channels: int = DEFAULT_CHANNELS,
    ) -> None:
        self._sample_rate = sample_rate
        self._channels = channels

    async def extract(
        self,
        video_path: Path,
        output_wav: Path,
        *,
        has_audio: bool = True,
    ) -> ExtractedAudio:
        """Run ffmpeg, return a small ExtractedAudio descriptor.

        Raises AudioExtractionError if the source has no audio or
        ffmpeg fails.
        """
        if not has_audio:
            raise AudioExtractionError("Source has no audio track")

        output_wav.parent.mkdir(parents=True, exist_ok=True)

        media = get_media_service()
        try:
            out = await media.extract_audio(
                video_path,
                output_wav,
                sample_rate=self._sample_rate,
                channels=self._channels,
            )
        except FileNotFoundError as e:
            raise AudioExtractionError(str(e)) from e
        except RuntimeError as e:
            raise AudioExtractionError(f"FFmpeg failed: {e}") from e

        size = out.stat().st_size
        # 16-bit mono @ sample_rate → bytes = duration_seconds * sample_rate * 2
        duration = size / (self._sample_rate * self._channels * 2)
        return ExtractedAudio(
            path=out,
            duration_seconds=duration,
            sample_rate=self._sample_rate,
            channels=self._channels,
        )