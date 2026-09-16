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
        filename: str = "input.mp4",
    ) -> Path:
        """Download and return the absolute path to the saved file.

        `source` can be a Message (the bot extracts the biggest video
        attachment internally) or an already-fetched aiogram File.
        """
        job_dir.mkdir(parents=True, exist_ok=True)
        target = job_dir / filename

        # If a Message was passed, get the biggest video attachment.
        file: File
        if isinstance(source, File):
            file = source
        else:
            file = await self._resolve_video_file(source)

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
    async def _resolve_video_file(message: Message) -> File:
        """Pick the biggest video attachment on the message."""
        candidates = []
        if message.video:
            candidates.append(("video", message.video))
        if message.video_note:
            candidates.append(("video_note", message.video_note))
        if message.animation:  # sometimes videos come as GIFs / animations
            candidates.append(("animation", message.animation))
        if message.document and message.document.mime_type and message.document.mime_type.startswith("video/"):
            candidates.append(("document", message.document))

        if not candidates:
            raise ValueError("Message has no video attachment")

        # Largest by file_size wins.
        candidates.sort(key=lambda c: (getattr(c[1], "file_size", 0) or 0), reverse=True)
        _kind, attachment = candidates[0]

        return await message.bot.get_file(attachment.file_id)
