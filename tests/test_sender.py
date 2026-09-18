"""Tests for Stage K (TelegramSender).

We mock the aiogram Bot to verify the sender pushes the right call
shape (send_video with the right args) and tracks successes / failures.
"""
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.pipeline.final_renderer import FinalClip, FinalJob
from app.services.sender import TelegramSender, SenderError


def _make_clip(index: int, path: Path, has_subs: bool = True, has_cta: bool = True) -> FinalClip:
    return FinalClip(
        index=index,
        final_path=path,
        has_subtitles=has_subs,
        has_cta=has_cta,
        size_bytes=path.stat().st_size if path.exists() else 1024,
    )


def test_send_video_happy_path(tmp_path: Path):
    # Create two small MP4-like files
    p1 = tmp_path / "a.mp4"
    p2 = tmp_path / "b.mp4"
    p1.write_bytes(b"\x00" * 200)
    p2.write_bytes(b"\x00" * 200)

    final_job = FinalJob(clips=(
        _make_clip(1, p1),
        _make_clip(2, p2),
    ))

    bot = MagicMock()
    sent_paths: list[Path] = []

    async def fake_send_video(**kwargs):
        m = MagicMock()
        m.message_id = len(sent_paths) + 100
        # InputFile.path is the underlying Path we passed in.
        sent_paths.append(Path(kwargs["video"].path))
        return m

    bot.send_video = AsyncMock(side_effect=fake_send_video)

    async def _run():
        return await TelegramSender(bot).send(final_job, chat_id=42)

    result = asyncio.run(_run())

    assert len(result.sent) == 2
    assert result.failed == ()
    assert bot.send_video.call_count == 2
    assert sent_paths == [p1, p2]


def test_send_video_continues_on_failure(tmp_path: Path):
    p1 = tmp_path / "a.mp4"
    p2 = tmp_path / "b.mp4"
    p1.write_bytes(b"\x00" * 200)
    p2.write_bytes(b"\x00" * 200)

    final_job = FinalJob(clips=(
        _make_clip(1, p1),
        _make_clip(2, p2),
    ))

    bot = MagicMock()

    async def send_with_fail(**kwargs):
        if Path(kwargs["video"].path).name == "a.mp4":
            raise RuntimeError("telegram boom")
        m = MagicMock()
        m.message_id = 42
        return m

    bot.send_video = AsyncMock(side_effect=send_with_fail)

    async def _run():
        return await TelegramSender(bot).send(final_job, chat_id=1)

    result = asyncio.run(_run())

    assert len(result.sent) == 1
    assert result.failed == (1,)
    assert result.sent[0].index == 2


def test_send_video_missing_file(tmp_path: Path):
    """Clip with a missing final_path goes to failed, doesn't crash the loop."""
    missing = tmp_path / "does-not-exist.mp4"
    valid = tmp_path / "ok.mp4"
    valid.write_bytes(b"\x00" * 100)

    final_job = FinalJob(clips=(
        _make_clip(1, missing),
        _make_clip(2, valid),
    ))
    bot = MagicMock()
    bot.send_video = AsyncMock(return_value=MagicMock(message_id=1))

    async def _run():
        return await TelegramSender(bot).send(final_job, chat_id=1)

    result = asyncio.run(_run())
    assert len(result.sent) == 1
    assert result.failed == (1,)


def test_caption_truncated_to_telegram_limit(tmp_path: Path):
    """Ensure caption stays <= 1024 chars even with long flags."""
    p = tmp_path / "a.mp4"
    p.write_bytes(b"\x00" * 100)

    final_job = FinalJob(clips=(
        _make_clip(99, p),
    ))
    bot = MagicMock()
    captured: dict = {}

    async def capture(**kwargs):
        captured.update(kwargs)
        m = MagicMock()
        m.message_id = 1
        return m

    bot.send_video = AsyncMock(side_effect=capture)

    async def _run():
        return await TelegramSender(bot).send(final_job, chat_id=1)

    asyncio.run(_run())
    assert "caption" in captured
    assert len(captured["caption"]) <= 1024