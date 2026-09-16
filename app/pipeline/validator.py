"""Video validation: file size, duration, supported MIME types.

All checks happen against the local `Settings` (max_video_size_mb,
max_video_duration_minutes). The validator never touches disk — caller
passes file size in bytes and duration in seconds.
"""
from dataclasses import dataclass
from typing import Iterable

from app.core.config import get_settings


class VideoValidationError(ValueError):
    """Raised when a video fails validation.

    `code` is a short machine-readable token for logging / i18n.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(slots=True, frozen=True)
class VideoMeta:
    """Subset of ffprobe metadata needed at the validation step."""
    duration_seconds: float
    width: int
    height: int
    has_audio: bool
    mime_type: str


# Telegram Bot API supports these for `video` messages.
SUPPORTED_MIME_TYPES: frozenset[str] = frozenset({
    "video/mp4",
    "video/quicktime",   # .mov
    "video/x-matroska",  # .mkv
    "video/webm",
})


class VideoValidator:
    """Pure validator — easy to unit-test without Telegram."""

    def __init__(
        self,
        max_size_bytes: int | None = None,
        max_duration_seconds: float | None = None,
        allowed_mime_types: Iterable[str] | None = None,
    ) -> None:
        s = get_settings()
        self._max_size = max_size_bytes or s.max_video_size_mb * 1024 * 1024
        self._max_duration = max_duration_seconds or s.max_video_duration_minutes * 60
        self._allowed_mime = frozenset(allowed_mime_types or SUPPORTED_MIME_TYPES)

    def check_size(self, size_bytes: int) -> None:
        if size_bytes <= 0:
            raise VideoValidationError("EMPTY_FILE", "Файл пустой.")
        if size_bytes > self._max_size:
            mb = size_bytes / (1024 * 1024)
            raise VideoValidationError(
                "FILE_TOO_LARGE",
                f"Видео {mb:.1f} МБ превышает лимит "
                f"({self._max_size // (1024 * 1024)} МБ).",
            )

    def check_mime(self, mime_type: str | None) -> None:
        if not mime_type:
            # Telegram sometimes omits mime; we accept and let probe decide.
            return
        # Telegram sometimes sends application/octet-stream; accept those.
        if mime_type == "application/octet-stream":
            return
        if mime_type not in self._allowed_mime:
            raise VideoValidationError(
                "UNSUPPORTED_FORMAT",
                f"Формат {mime_type} не поддерживается. "
                f"Отправь MP4, MOV, MKV или WebM.",
            )

    def check_duration(self, duration_seconds: float) -> None:
        if duration_seconds <= 0:
            raise VideoValidationError("ZERO_DURATION", "Не удалось определить длительность.")
        if duration_seconds > self._max_duration:
            minutes = duration_seconds / 60
            limit = self._max_duration / 60
            raise VideoValidationError(
                "TOO_LONG",
                f"Видео {minutes:.1f} мин превышает лимит ({limit:.0f} мин).",
            )

    def check_meta(self, meta: VideoMeta) -> None:
        """Run all metadata-related checks (duration)."""
        self.check_duration(meta.duration_seconds)
