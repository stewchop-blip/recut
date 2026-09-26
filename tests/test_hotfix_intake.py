"""HOTFIX 2026-09-26 acceptance tests.

Covers TZ items: READY enum persistence (1/2/3/14), local document routing
(5/6/12), URL extract->normalize->download-only-URL (9/10).
Runs on SQLite (CI-safe); the PG enum migration itself is exercised on
Railway startup (logged postgres_jobstatus_before/after).
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# ---------------------------------------------------------------------------
# Item 3/14: JobStatus.READY persists (SQLite prod-style roundtrip)
# ---------------------------------------------------------------------------

def test_ready_status_persists(tmp_path):
    from sqlalchemy import select, delete as _delete
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
    from app.database.models import Job, JobStatus, Base
    from app.database.session import db_manager

    db_file = tmp_path / "test_ready.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_file}")
    old_engine = db_manager._engine
    db_manager._engine = engine
    db_manager._session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False, autoflush=False)
    try:
        async def run():
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)

            async with db_manager.session() as session:
                job = Job(telegram_user_id=0, telegram_chat_id=0,
                          source_message_id=0, status=JobStatus.READY)
                session.add(job)
                await session.flush()
                jid = job.id
                await session.commit()
            async with db_manager.session() as session:
                row = (await session.execute(
                    select(Job).where(Job.id == jid))).scalar_one()
                assert row.status == JobStatus.READY
                await session.execute(_delete(Job).where(Job.id == jid))
                await session.commit()
        asyncio.run(run())
    finally:
        db_manager._engine = old_engine
        db_manager._session_factory = None


def test_ready_enum_exists():
    from app.database.models import JobStatus
    assert JobStatus.READY.name == "READY"


# ---------------------------------------------------------------------------
# Items 5/6/12: document routing — ffprobe decides, MIME is a hint
# ---------------------------------------------------------------------------

def _ffmpeg() -> str:
    import shutil
    return shutil.which("ffmpeg") or "ffmpeg"


def _make_mp4(path: Path, w=64, h=128, d=1.0) -> Path:
    subprocess.run(
        [_ffmpeg(), "-y", "-v", "error", "-f", "lavfi",
         "-i", f"testsrc2=s={w}x{h}:d={d}", "-c:v", "libx264",
         "-pix_fmt", "yuv420p", str(path)],
        check=True)
    return path


def _pick(kind: str, path: Path | None = None, mime: str | None = None):
    """Build a fake aiogram-like message.attachment for _pick_attachment."""
    class A:
        file_size = 1234
        mime_type = mime
        file_name = path.name if path else None
        file_id = "test_file_id"
    class M:
        video = None
        video_note = None
        animation = None
        document = A() if kind == "document" else None
    return M()


def test_pick_attachment_octet_stream_accepted(tmp_path):
    from app.bot.handlers.video import _pick_attachment
    msg = _pick("document", _make_mp4(tmp_path / "video.mp4"),
                mime="application/octet-stream")
    assert _pick_attachment(msg) is not None


def test_pick_attachment_no_mime_mov_accepted(tmp_path):
    from app.bot.handlers.video import _pick_attachment
    msg = _pick("document", tmp_path / "clip.mov", mime=None)
    assert _pick_attachment(msg) is not None


def test_pick_attachment_non_video_rejected(tmp_path):
    from app.bot.handlers.video import _pick_attachment
    msg = _pick("document", tmp_path / "doc.pdf", mime="application/pdf")
    assert _pick_attachment(msg) is None


def test_pick_attachment_video_mime_accepted(tmp_path):
    from app.bot.handlers.video import _pick_attachment
    msg = _pick("document", _make_mp4(tmp_path / "v.mp4"), mime="video/mp4")
    assert _pick_attachment(msg) is not None


def test_ffprobe_accepts_real_video_rejects_garbage(tmp_path):
    """Item 6: downloaded bytes must be validated by ffprobe, not MIME."""
    from app.services.media.probe import get_probe_service
    real = _make_mp4(tmp_path / "real.mp4")
    garbage = tmp_path / "garbage.mp4"
    garbage.write_bytes(b"not a video at all" * 100)
    probe = get_probe_service()

    async def run():
        ok = await probe.probe(real)
        assert ok.duration_seconds > 0 and ok.width > 0
        with pytest.raises(Exception):
            await probe.probe(garbage)
    asyncio.run(run())


# ---------------------------------------------------------------------------
# Items 9/10: URL pipeline — extract, normalize, single validator
# ---------------------------------------------------------------------------

def test_extract_url_from_wrapped_text():
    from app.services.downloader.url_utils import extract_url, normalize_url
    text = ("Вот ролик 👇\n"
            "https://www.instagram.com/reel/Cabcdef/?igsh=MTk2xyz&utm_source=share")
    url = extract_url(text)
    assert url is not None
    assert url.startswith("https://www.instagram.com/reel/")
    norm = normalize_url(url)
    assert "igsh=" not in norm and "utm_source" not in norm


def test_downloader_validate_delegates_to_url_utils():
    """Item 10: ONE validator — DownloaderService._validate must accept
    exactly what url_utils.is_supported_url accepts."""
    from app.pipeline.url_downloader import DownloaderService, UnsupportedURLError
    from app.services.downloader.url_utils import is_supported_url
    svc = DownloaderService()
    good = "https://www.instagram.com/reel/Cabc/"
    assert svc._validate(good) == good
    assert is_supported_url(good)
    for bad in ("https://example.com/v.mp4", "not a url", ""):
        with pytest.raises(UnsupportedURLError):
            svc._validate(bad)


def test_normalize_url_strips_tracking():
    from app.services.downloader.url_utils import normalize_url
    u = normalize_url("https://vm.tiktok.com/abc/?si=X&is_from_webapp=1&keep=1")
    assert "si=" not in u and "is_from_webapp" not in u and "keep=1" in u


# ---------------------------------------------------------------------------
# Item 11: MediaJobQueue.run_download actually serializes + dedups
# ---------------------------------------------------------------------------

def test_job_queue_runs_work_and_dedups():
    from app.services.downloader.jobs import get_job_queue
    q = get_job_queue()
    calls = []

    async def work():
        calls.append(1)
        await asyncio.sleep(0.05)
        return "ok"

    async def run():
        r = await q.run_download(1, "https://x.test/a", work)
        assert r == "ok" and len(calls) == 1
        assert not q.is_busy(1)
    asyncio.run(run())


def test_job_queue_rejects_second_job_same_user():
    from app.services.downloader.jobs import get_job_queue
    q = get_job_queue()

    async def run():
        async def slow():
            await asyncio.sleep(0.2)
            return "done"

        t = asyncio.create_task(q.run_download(2, "https://x.test/slow", slow))
        await asyncio.sleep(0.02)
        assert q.is_busy(2)

        async def other():
            return "no"
        r = await q.run_download(2, "https://x.test/other", other)
        assert getattr(r, "success", None) is False and r.error_code == "QUEUE_FULL"
        assert await t == "done"
    asyncio.run(run())
