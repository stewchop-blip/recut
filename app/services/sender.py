"""Telegram sender — Stage K.

Reads the final clips on disk and ships them to the user via the
Telegram Bot API. We use `bot.send_video` so files render inline in the
chat (vs. as a file download). Each clip carries a caption with its
title and hook, plus a job ID for traceability.

After successful delivery we schedule cleanup of the per-job temp
directory (handled by TempFileManager.job_context on the call site).
"""
from dataclasses import dataclass
from pathlib import Path

from aiogram import Bot
from aiogram.types import FSInputFile, Message

from app.core.logging import get_logger
from app.pipeline.final_renderer import FinalClip, FinalJob

logger = get_logger(__name__)


class SenderError(RuntimeError):
    """Telegram delivery failed in a non-recoverable way."""


@dataclass(slots=True, frozen=True)
class SentClip:
    """One clip successfully delivered to Telegram."""
    index: int
    message_id: int


@dataclass(slots=True, frozen=True)
class SendJob:
    """Result of one delivery run."""
    sent: tuple[SentClip, ...]
    failed: tuple[int, ...]      # indices that couldn't be sent


class TelegramSender:
    """Send `FinalJob` clips via aiogram's send_video."""

    # Telegram Bot API limit ~50 MB; compress at 48 MB to leave headroom
    _MAX_SAFE_BYTES = 48 * 1024 * 1024

    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def send(
        self,
        final_job: FinalJob,
        chat_id: int,
        *,
        reply_to_message_id: int | None = None,
    ) -> SendJob:
        """Send every clip; collect successes and failures."""
        sent: list[SentClip] = []
        failed: list[int] = []

        for clip in final_job.clips:
            try:
                msg = await self._send_one(clip, chat_id, reply_to_message_id)
            except Exception as e:
                logger.error(
                    "send_clip_failed",
                    index=clip.index,
                    error=str(e)[:200],
                )
                failed.append(clip.index)
                continue
            sent.append(SentClip(
                index=clip.index,
                message_id=msg.message_id,
            ))

        logger.info(
            "send_job_done",
            sent=len(sent),
            failed=len(failed),
        )
        return SendJob(sent=tuple(sent), failed=tuple(failed))

    async def _send_one(
        self,
        clip: FinalClip,
        chat_id: int,
        reply_to_message_id: int | None,
    ) -> Message:
        """Upload + caption one clip. Auto-compress if >48 MB."""
        if not clip.final_path.exists():
            raise SenderError(f"final file missing: {clip.final_path}")

        send_path = clip.final_path
        # Telegram Bot API limit: 50 MB. Compress if needed.
        size_bytes = send_path.stat().st_size
        if size_bytes > self._MAX_SAFE_BYTES:
            logger.warning(
                "sender_compressing",
                original_size=size_bytes,
                path=str(send_path),
            )
            send_path = await self._compress_for_telegram(send_path)

        caption = _build_caption(clip, send_path)
        kwargs = {
            "chat_id": chat_id,
            "video": FSInputFile(str(send_path), filename=send_path.name),
            "caption": caption,
            "supports_streaming": True,
        }
        if reply_to_message_id is not None:
            kwargs["reply_to_message_id"] = reply_to_message_id

        return await self._bot.send_video(**kwargs)

    async def _compress_for_telegram(self, src: Path) -> Path:
        """Re-encode to fit the Telegram limit — delegates to OutputCompressor
        (audit #41: ONE config for the limit, not hardcoded here)."""
        from app.services.output.compressor import get_output_compressor
        out = src.with_suffix(".tg.mp4")
        return await get_output_compressor().compress_to_target(src, out)


def _build_caption(clip: FinalClip, send_path: Path | None = None) -> str:
    """Telegram captions are HTML, 1024 char limit."""
    flags: list[str] = []
    if clip.has_subtitles:
        flags.append("субтитры")
    if clip.has_cta:
        flags.append("CTA")
    flag_str = " · ".join(flags)

    path = send_path or clip.final_path
    size_kb = path.stat().st_size // 1024
    return (
        f"🎬 <b>Клип {clip.index}</b>\n"
        f"{flag_str}\n"
        f"📦 {size_kb} КБ"
    )[:1024]