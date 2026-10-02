from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.core.config import get_settings
from app.database.models import Base, Acquisition, ActivityDay, UserSettings, Job
from app.database.repositories import JobRepository
from app.services.analytics import observe, record_result, report, source_from_payload, format_report
from app.services.referrals import ensure_profile

NOW = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)


@pytest.fixture
async def sessions(tmp_path, monkeypatch):
    cfg = get_settings()
    for field in ('admin_telegram_ids', 'tester_telegram_ids', 'allowed_telegram_user_ids', 'unlimited_telegram_ids'):
        monkeypatch.setattr(cfg, field, '')
    engine = create_async_engine(f'sqlite+aiosqlite:///{tmp_path}/analytics.db')
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def test_first_touch_replay_legacy_and_referral_compatibility(sessions):
    async with sessions.begin() as s:
        parent, _ = await ensure_profile(s, 99)
        payload = f'ref_{parent.invite_token}'
        await observe(s, 1, 'src_tiktok', now=NOW)
        await observe(s, 1, 'src_instagram', now=NOW)
        await observe(s, 2, payload, now=NOW)
        child, _ = await ensure_profile(s, 2, payload)
        assert child.inviter_id == 99
        s.add(UserSettings(telegram_user_id=3))
        await s.flush()
        await observe(s, 3, 'src_youtube', now=NOW)
        assert not (await s.get(Acquisition, 3)).is_new
        assert (await s.get(Acquisition, 1)).source == 'tiktok'
        assert (await s.get(Acquisition, 2)).source == 'referral'
        assert await s.scalar(select(func.count()).select_from(ActivityDay)) == 3


async def test_cohort_denominators_mature_days_and_test_exclusion(sessions, monkeypatch):
    monkeypatch.setattr(get_settings(), 'allowed_telegram_user_ids', '99')
    async with sessions.begin() as s:
        day0 = NOW - timedelta(days=10)
        await observe(s, 1, 'src_tiktok', now=day0)
        await observe(s, 1, now=day0+timedelta(days=1))
        await observe(s, 1, now=day0+timedelta(days=7))
        await record_result(s, 1, now=day0)
        await record_result(s, 1, now=NOW)
        await observe(s, 2, 'src_tiktok', now=NOW)  # D1/D7 not mature
        await observe(s, 3, 'src_instagram', now=day0)  # mature non-returner
        await observe(s, 99, 'src_tiktok', now=day0)  # internal excluded
        s.add(UserSettings(telegram_user_id=4))
        await s.flush()
        await observe(s, 4, now=day0)  # legacy active, not new cohort
        data = await report(s, now=NOW)
        assert data['total'] == (3, 1, 2, 1, 2, 1)
        assert data['active'] == 4
        assert (await report(s, source='tiktok', now=NOW))['total'] == (2, 1, 1, 1, 1, 1)
        first = (await s.get(Acquisition, 1)).first_result_at
        assert first.replace(tzinfo=timezone.utc) == day0
        assert '1/2 (50%)' in format_report(data)


async def test_current_return_day_not_yet_in_denominator(sessions):
    async with sessions.begin() as s:
        await observe(s, 1, now=NOW-timedelta(days=1))
        await observe(s, 1, now=NOW)
        assert (await report(s, now=NOW))['total'][2:4] == (0, 0)
        assert (await report(s, now=NOW+timedelta(days=1)))['total'][2:4] == (1, 1)


async def test_job_delivery_hook_excludes_downloads(sessions):
    async with sessions.begin() as s:
        await observe(s, 1, now=NOW)
        job = Job(telegram_user_id=1, telegram_chat_id=1, source_message_id=1)
        s.add(job)
        await s.flush()
        repo = JobRepository(s)
        await repo.mark_completed(job.id, 0)
        assert (await s.get(Acquisition, 1)).first_result_at is None
        await repo.mark_completed(job.id, 3)
        assert (await s.get(Acquisition, 1)).first_result_at is not None


async def test_private_admin_only_and_validated_links(sessions, monkeypatch):
    from app.bot.handlers.admin import cmd_stats, cmd_links
    monkeypatch.setattr(get_settings(), 'admin_telegram_ids', '10')
    msg = SimpleNamespace(chat=SimpleNamespace(type='private'), from_user=SimpleNamespace(id=20),
                          text='/stats', answer=AsyncMock())
    await cmd_stats(msg)
    await cmd_links(msg)
    msg.answer.assert_not_awaited()
    msg.from_user.id = 10
    msg.chat.type = 'group'
    await cmd_stats(msg)
    msg.answer.assert_not_awaited()
    msg.chat.type = 'private'
    msg.text = '/links <script>'
    await cmd_links(msg)
    assert 'Пример' in msg.answer.call_args.args[0]


def test_payload_validation():
    assert source_from_payload('src_tiktok_video_01') == 'tiktok_video_01'
    assert source_from_payload('src_'+('x'*49)) == 'direct'
    assert source_from_payload('src_<a>') == 'direct'
    assert source_from_payload('unknown') == 'direct'


async def test_analytics_failure_does_not_block_handler(monkeypatch):
    from aiogram.types import Message, User, Chat
    from app.bot.middlewares.analytics import AnalyticsMiddleware
    from app.database.session import db_manager
    @asynccontextmanager
    async def broken():
        raise RuntimeError('database offline')
        yield
    monkeypatch.setattr(db_manager, 'session', broken)
    msg = Message(message_id=1, date=NOW, chat=Chat(id=10, type='private'),
                  from_user=User(id=10, is_bot=False, first_name='Test'), text='/start src_tiktok')
    handler = AsyncMock(return_value='ok')
    assert await AnalyticsMiddleware()(handler, msg, {}) == 'ok'
    handler.assert_awaited_once()


async def test_router_reaches_admin_before_broad_video_handler(sessions, monkeypatch):
    from aiogram import Bot
    from aiogram.types import Update, Message, User, Chat
    from app.database.session import db_manager
    from app.main import _build_dispatcher
    monkeypatch.setattr(get_settings(), 'allowed_telegram_user_ids', '10')
    @asynccontextmanager
    async def session():
        async with sessions.begin() as s:
            yield s
    monkeypatch.setattr(db_manager, 'session', session)
    bot = Bot('123456789:test-placeholder')
    monkeypatch.setattr(bot.session, 'make_request', AsyncMock(return_value=True))
    # Router instances are shared; detach at the end to avoid leaking into other tests.
    dispatcher = _build_dispatcher()
    try:
        await dispatcher.feed_update(bot, Update(update_id=1, message=Message(
            message_id=1, date=NOW, chat=Chat(id=10, type='private'),
            from_user=User(id=10, is_bot=False, first_name='Test'), text='/stats')))
        sent = bot.session.make_request.call_args.args[1]
        assert 'Новых пользователей: 0' in sent.text
    finally:
        for router in list(dispatcher.sub_routers):
            router._parent_router = None
        dispatcher.sub_routers.clear()
        await bot.session.close()
