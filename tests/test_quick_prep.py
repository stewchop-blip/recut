"""Tests for Quick Prep pipeline and UserSettingsRepository."""
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _ffmpeg_available() -> bool:
    import shutil
    return shutil.which("ffmpeg") is not None


# ---------------------------------------------------------------------------
# QuickPrepPipeline (real ffmpeg path, skipped if unavailable)
# ---------------------------------------------------------------------------

def _make_test_video(path: Path, duration: float = 2.0, width: int = 320, height: int = 240) -> None:
    import shutil, subprocess
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"sine=frequency=1000:duration={duration}",
        "-f", "lavfi", "-i", f"color=c=blue:s={width}x{height}:d={duration}",
        "-shortest",
        "-c:v", "libx264", "-c:a", "aac",
        "-pix_fmt", "yuv420p",
        str(path),
    ]
    if subprocess.run(cmd, capture_output=True).returncode != 0:
        pytest.skip("ffmpeg fixture failed")


@pytest.mark.skipif(not _ffmpeg_available(), reason="ffmpeg not installed")
def test_quick_prep_runs_landscape(tmp_path: Path):
    from app.pipeline.quick_prep import QuickPrepPipeline
    src = tmp_path / "input.mp4"
    _make_test_video(src, duration=2.0)
    job_dir = tmp_path / "job"

    async def _run():
        p = QuickPrepPipeline()
        return await p.run(
            input_video=src,
            job_dir=job_dir,
            target_width=540, target_height=960, target_fps=30,
            video_bitrate="2M", audio_bitrate="96k",
            cta_asset=None, cta_position="bottom", cta_mode="end",
            cta_duration_seconds=2.0, cta_start_seconds=0.0,
            cta_min_margin_px=60,
            output_width=540, output_height=960,
        )

    result = asyncio.run(_run())
    assert result.final_path.exists()
    assert result.size_bytes > 0
    assert result.width == 540
    assert result.height == 960
    # CTA was disabled so final output == vertical output
    assert result.has_cta is False


@pytest.mark.skipif(not _ffmpeg_available(), reason="ffmpeg not installed")
def test_quick_prep_with_cta(tmp_path: Path):
    from app.pipeline.quick_prep import QuickPrepPipeline
    from PIL import Image, ImageDraw

    src = tmp_path / "input.mp4"
    _make_test_video(src, duration=3.0)

    # Create a small CTA banner PNG
    cta = tmp_path / "cta.png"
    img = Image.new("RGBA", (400, 100), (255, 0, 0, 200))
    d = ImageDraw.Draw(img)
    d.text((10, 30), "TEST CTA", fill=(255, 255, 255, 255))
    img.save(cta, "PNG")

    job_dir = tmp_path / "job"

    async def _run():
        p = QuickPrepPipeline()
        return await p.run(
            input_video=src,
            job_dir=job_dir,
            target_width=540, target_height=960, target_fps=30,
            video_bitrate="2M", audio_bitrate="96k",
            cta_asset=cta, cta_position="bottom", cta_mode="end",
            cta_duration_seconds=1.5, cta_start_seconds=0.0,
            cta_min_margin_px=60,
            output_width=540, output_height=960,
        )

    result = asyncio.run(_run())
    assert result.has_cta is True
    assert result.final_path.exists()


@pytest.mark.skipif(not _ffmpeg_available(), reason="ffmpeg not installed")
def test_quick_prep_passthrough_portrait(tmp_path: Path):
    from app.pipeline.quick_prep import QuickPrepPipeline

    # 540x960 is already 9:16 — pipeline should passthrough without re-encoding vertical.
    src = tmp_path / "input.mp4"
    _make_test_video(src, duration=2.0, width=540, height=960)
    job_dir = tmp_path / "job"

    async def _run():
        p = QuickPrepPipeline()
        return await p.run(
            input_video=src,
            job_dir=job_dir,
            target_width=540, target_height=960, target_fps=30,
            video_bitrate="2M", audio_bitrate="96k",
            cta_asset=None, cta_position="bottom", cta_mode="end",
            cta_duration_seconds=2.0, cta_start_seconds=0.0,
            cta_min_margin_px=60,
            output_width=540, output_height=960,
        )

    result = asyncio.run(_run())
    # Should be 540x960 either way
    assert result.width == 540
    assert result.height == 960


def test_quick_prep_missing_input_raises(tmp_path: Path):
    from app.pipeline.quick_prep import QuickPrepPipeline, QuickPrepError

    async def _run():
        p = QuickPrepPipeline()
        await p.run(
            input_video=tmp_path / "nope.mp4",
            job_dir=tmp_path / "job",
            target_width=540, target_height=960, target_fps=30,
            video_bitrate="2M", audio_bitrate="96k",
            cta_asset=None, cta_position="bottom", cta_mode="end",
            cta_duration_seconds=2.0, cta_start_seconds=0.0,
            cta_min_margin_px=60,
            output_width=540, output_height=960,
        )

    with pytest.raises(QuickPrepError, match="missing"):
        asyncio.run(_run())


# ---------------------------------------------------------------------------
# CTA _make_spec
# ---------------------------------------------------------------------------

def test_cta_make_spec_position_variants():
    from app.services.overlays.cta import CTAService
    from pathlib import Path
    svc = CTAService()
    asset = Path("/tmp/fake.png")
    for pos in ["top", "bottom", "top_left", "top_right",
                "bottom_left", "bottom_right"]:
        spec = svc._make_spec(
            clip_duration=10.0, mode="end", duration_seconds=3.0,
            start_seconds=0.0, position=pos, margin=80,
            output_w=1080, output_h=1920, asset=asset,
        )
        assert spec is not None, pos
        assert spec.asset_path == asset
        assert spec.start_seconds == 7.0   # end mode = clip_duration - duration
        assert spec.end_seconds == 10.0


def test_cta_make_spec_timing_variants():
    from app.services.overlays.cta import CTAService
    from pathlib import Path
    svc = CTAService()
    asset = Path("/tmp/fake.png")
    # full
    s = svc._make_spec(clip_duration=10.0, mode="full", duration_seconds=3.0,
                       start_seconds=0.0, position="bottom", margin=80,
                       output_w=1080, output_h=1920, asset=asset)
    assert s.start_seconds == 0.0 and s.end_seconds == 10.0
    # start
    s = svc._make_spec(clip_duration=10.0, mode="start", duration_seconds=3.0,
                       start_seconds=0.0, position="bottom", margin=80,
                       output_w=1080, output_h=1920, asset=asset)
    assert s.start_seconds == 0.0 and s.end_seconds == 3.0
    # range
    s = svc._make_spec(clip_duration=10.0, mode="range", duration_seconds=2.0,
                       start_seconds=5.0, position="bottom", margin=80,
                       output_w=1080, output_h=1920, asset=asset)
    assert s.start_seconds == 5.0 and s.end_seconds == 7.0


def test_cta_make_spec_unknown_mode_returns_none():
    from app.services.overlays.cta import CTAService
    from pathlib import Path
    svc = CTAService()
    spec = svc._make_spec(
        clip_duration=10.0, mode="garbage", duration_seconds=3.0,
        start_seconds=0.0, position="bottom", margin=80,
        output_w=1080, output_h=1920, asset=Path("/tmp/fake.png"),
    )
    assert spec is None


def test_cta_make_spec_unknown_position_returns_none():
    from app.services.overlays.cta import CTAService
    from pathlib import Path
    svc = CTAService()
    spec = svc._make_spec(
        clip_duration=10.0, mode="end", duration_seconds=3.0,
        start_seconds=0.0, position="garbage", margin=80,
        output_w=1080, output_h=1920, asset=Path("/tmp/fake.png"),
    )
    assert spec is None


# ---------------------------------------------------------------------------
# UserSettings dataclass defaults
# ---------------------------------------------------------------------------

def test_user_settings_defaults():
    """Defaults are applied by SQLAlchemy at flush time, not on __init__."""
    from app.database.models import UserSettings
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    s = UserSettings(telegram_user_id=123)
    # Direct attribute read returns None for non-loaded mapped defaults;
    # flush against an in-memory SQLite exposes the column defaults.
    engine = create_engine("sqlite:///:memory:")
    from app.database.models import Base
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as session:
        session.add(s)
        session.flush()
        assert s.cta_enabled is False
        assert s.cta_position == "bottom"
        assert s.cta_mode == "end"
        assert s.cta_duration_seconds == 4.0
        assert s.cta_start_seconds == 0.0
        assert s.subtitles_enabled is False
        assert s.output_mode == "universal_9_16"


# ---------------------------------------------------------------------------
# Stale Job cleanup (regression for "stuck PENDING blocks new video" bug)
# ---------------------------------------------------------------------------

def test_stale_jobs_are_cleaned_up():
    """A PENDING Job older than max_age_minutes must be marked FAILED
    by cleanup_stale_jobs so it stops blocking has_active_job.

    Note: JobRepository methods are async, so we test against an
    AsyncSession backed by aiosqlite (in-memory).
    """
    from datetime import datetime, timedelta, timezone

    from app.database.models import Base, Job, JobStatus
    from app.database.repositories import JobRepository
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    import asyncio

    async def _run():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

        async with Session() as session:
            fresh = Job(
                telegram_user_id=42, telegram_chat_id=42, source_message_id=1,
                status=JobStatus.PENDING, created_at=datetime.now(timezone.utc),
            )
            stale = Job(
                telegram_user_id=42, telegram_chat_id=42, source_message_id=2,
                status=JobStatus.CUTTING,
                created_at=datetime.now(timezone.utc) - timedelta(hours=2),
            )
            session.add_all([fresh, stale])
            await session.commit()

            repo = JobRepository(session)
            assert await repo.has_active_job(42, max_age_minutes=30) is True

            n = await repo.cleanup_stale_jobs(max_age_minutes=30)
            assert n == 1
            await session.commit()

            assert await repo.has_active_job(42, max_age_minutes=30) is True
            from sqlalchemy import update as _u
            await session.execute(
                _u(Job).where(Job.id == fresh.id).values(status=JobStatus.COMPLETED)
            )
            await session.commit()
            assert await repo.has_active_job(42, max_age_minutes=30) is False

        await engine.dispose()

    asyncio.run(_run())


def test_old_completed_jobs_dont_count_as_active():
    """Terminal-status Jobs of any age must not block."""
    from datetime import datetime, timezone

    from app.database.models import Base, Job, JobStatus
    from app.database.repositories import JobRepository
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    import asyncio

    async def _run():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

        async with Session() as session:
            j = Job(
                telegram_user_id=99, telegram_chat_id=99, source_message_id=1,
                status=JobStatus.COMPLETED, created_at=datetime.now(timezone.utc),
            )
            session.add(j)
            await session.commit()
            repo = JobRepository(session)
            assert await repo.has_active_job(99) is False

        await engine.dispose()

    asyncio.run(_run())


def test_cancel_active_jobs_marks_them_cancelled():
    """cancel_active_jobs() turns in-flight rows into CANCELLED,
    so has_active_job() immediately returns False.
    """
    from datetime import datetime, timezone

    from app.database.models import Base, Job, JobStatus
    from app.database.repositories import JobRepository
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    import asyncio

    async def _run():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

        async with Session() as session:
            job = Job(
                telegram_user_id=7, telegram_chat_id=7, source_message_id=1,
                status=JobStatus.CUTTING, created_at=datetime.now(timezone.utc),
            )
            session.add(job)
            await session.commit()

            repo = JobRepository(session)
            assert await repo.has_active_job(7) is True

            n = await repo.cancel_active_jobs(7, max_age_minutes=10)
            assert n == 1
            await session.commit()

            assert await repo.has_active_job(7) is False

        await engine.dispose()

    asyncio.run(_run())