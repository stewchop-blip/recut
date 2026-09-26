"""Telegram → local filesystem downloader.

Streams a Telegram file directly to disk in 64 KB chunks, never holding
the whole file in memory. Uses `bot.download(file, destination=...)`
which honours the destination as a file path or a writable IO.
"""
import os
from pathlib import Path
from typing import Union

from aiogram import Bot
from aiogram.types import File, Message

from app.core.logging import get_logger

logger = get_logger(__name__)

# Chunk size for progress logging (1 MB)
_PROGRESS_EVERY_BYTES = 1024 * 1024


class VideoDownloader:
    """Downloads a Telegram video file to `job_dir/input.mp4`."""

    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def download_to_job_dir(
        self,
        source: Union[Message, File],
        job_dir: Path,
        filename: str | None = None,
    ) -> Path:
        """Download and return the absolute path to the saved file.

        `source` can be a Message (the bot extracts the biggest video
        attachment internally) or an already-fetched aiogram File.

        HOTFIX item 6: filename=None → the real extension is preserved from
        the Telegram attachment (input_source.mp4 / .mov / …). Never assume
        the bytes are MP4 just because a filename said so — ffprobe decides.
        """
        job_dir.mkdir(parents=True, exist_ok=True)

        # If a Message was passed, get the biggest video attachment.
        attachment = None
        file: File
        if isinstance(source, File):
            file = source
        else:
            file, attachment = await self._resolve_video_file(source)

        if filename:
            target = job_dir / filename
        else:
            ext = "mp4"
            if attachment is not None:
                real_name = getattr(attachment, "file_name", None) or ""
                mime = (getattr(attachment, "mime_type", "") or "").lower()
                candidates = []
                if real_name and "." in real_name:
                    candidates.append("." + real_name.rsplit(".", 1)[1].lower())
                mime_map = {
                    "video/mp4": ".mp4", "video/quicktime": ".mov",
                    "video/x-matroska": ".mkv", "video/webm": ".webm",
                    "video/x-msvideo": ".avi", "video/mpeg": ".mpg",
                }
                if mime in mime_map:
                    candidates.append(mime_map[mime])
                ext = next((c for c in candidates if c != "."), ".mp4").lstrip(".")
            target = job_dir / f"input_source.{ext}"

        await self._bot.download(file, destination=target)

        if not target.exists():
            raise RuntimeError(f"Download failed: {target} not found after bot.download")

        size = target.stat().st_size
        if size == 0:
            target.unlink(missing_ok=True)
            raise RuntimeError("Downloaded file is empty")

        logger.info(
            "video_downloaded",
            path=str(target),
            size_bytes=size,
            telegram_file_id=file.file_id,
        )
        return target

    @staticmethod
    async def _resolve_video_file(message: Message) -> tuple[File, object]:
        """Pick the biggest video attachment; MIME is a hint, not truth.

        Documents with octet-stream/None MIME are accepted — ffprobe after
        download decides whether the bytes are actually a video.
        Returns (fetched File, original attachment) so callers can recover
        the real filename/extension.
        """
        candidates = []
        if message.video:
            candidates.append(("video", message.video))
        if message.video_note:
            candidates.append(("video_note", message.video_note))
        if message.animation:  # sometimes videos come as GIFs / animations
            candidates.append(("animation", message.animation))
        doc = message.document
        if doc is not None:
            mime = (doc.mime_type or "").lower()
            fname = (doc.file_name or "").lower()
            video_exts = (".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v",
                          ".mpg", ".mpeg", ".ts", ".wmv", ".flv", ".3gp")
            if (mime.startswith("video/") or mime in ("", "application/octet-stream")
                    or mime.startswith("application/x-matroska")
                    or fname.endswith(video_exts)):
                candidates.append(("document", doc))

        if not candidates:
            raise ValueError("Message has no video attachment")

        # Largest by file_size wins.
        candidates.sort(key=lambda c: (getattr(c[1], "file_size", 0) or 0), reverse=True)
        _kind, attachment = candidates[0]

        return await message.bot.get_file(attachment.file_id), attachment
