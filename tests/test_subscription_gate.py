from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.database.models import Base, Job, JobStatus
from app.database.session import db_manager
from app.services.subscription import has_successful_processed_video, subscription_required


@pytest.fixture
async def sessions(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/subscription.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def session_context():
        async with factory() as session:
            yield session

    monkeypatch.setattr(db_manager, "session", session_context)
    yield factory
    await engine.dispose()


@pytest.fixture
def settings(monkeypatch):
    from app.core.config import get_settings
    cfg = get_settings()
    monkeypatch.setattr(cfg, "subscription_gate_enabled", True)
    monkeypatch.setattr(cfg, "subscription_channel", "@recutcontent")
    return cfg


async def test_only_successfully_processed_jobs_trigger_gate(sessions, settings):
    async with sessions.begin() as session:
        session.add(Job(telegram_user_id=42, telegram_chat_id=42, source_message_id=1,
                        status=JobStatus.COMPLETED, clips_generated=0))
        session.add(Job(telegram_user_id=43, telegram_chat_id=43, source_message_id=2,
                        status=JobStatus.FAILED, clips_generated=0))
        session.add(Job(telegram_user_id=44, telegram_chat_id=44, source_message_id=3,
                        status=JobStatus.COMPLETED, clips_generated=1))

    assert not await has_successful_processed_video(42)
    assert not await has_successful_processed_video(43)
    assert await has_successful_processed_video(44)

    bot = SimpleNamespace(get_chat_member=AsyncMock(return_value=SimpleNamespace(status="left")))
    assert not await subscription_required(bot, 42)
    assert await subscription_required(bot, 44)
    bot.get_chat_member.assert_awaited_once_with("@recutcontent", 44)


async def test_gate_accepts_active_members_and_admins(sessions, settings):
    async with sessions.begin() as session:
        session.add(Job(telegram_user_id=50, telegram_chat_id=50, source_message_id=4,
                        status=JobStatus.COMPLETED, clips_generated=1))
    bot = SimpleNamespace(get_chat_member=AsyncMock(return_value=SimpleNamespace(status="member")))
    assert not await subscription_required(bot, 50)
    bot.get_chat_member.return_value = SimpleNamespace(status="administrator")
    assert not await subscription_required(bot, 50)


async def test_check_failure_does_not_silently_bypass_gate(sessions, settings):
    async with sessions.begin() as session:
        session.add(Job(telegram_user_id=60, telegram_chat_id=60, source_message_id=5,
                        status=JobStatus.COMPLETED, clips_generated=1))
    bot = SimpleNamespace(get_chat_member=AsyncMock(side_effect=RuntimeError("bad config")))
    with pytest.raises(RuntimeError, match="subscription check unavailable"):
        await subscription_required(bot, 60)


async def test_kill_switch_skips_membership_api(sessions, settings, monkeypatch):
    settings.subscription_gate_enabled = False
    bot = SimpleNamespace(get_chat_member=AsyncMock())
    assert not await subscription_required(bot, 70)
    bot.get_chat_member.assert_not_awaited()
