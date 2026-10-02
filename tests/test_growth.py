"""Referrals and allowance persist; duplicate deliveries never multiply rewards."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.database.models import Base, BotProfile, CreditEntry, Job, JobStatus, GenerationRun, UserSettings
from app.database.repositories import JobRepository
from app.services import referrals
from app.services.referrals import ensure_profile, qualify_referral
from app.services.generations import reserve, refund_unfinished, current_run, AllowanceRejected
from app.services.payments import balance


@pytest.fixture
async def sessions(tmp_path, monkeypatch):
    engine = create_async_engine(f'sqlite+aiosqlite:///{tmp_path}/growth.db')
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture
def settings(monkeypatch):
    from app.core.config import get_settings
    cfg = get_settings()
    monkeypatch.setattr(cfg, 'generation_limits_enabled', False)
    monkeypatch.setattr(cfg, 'referrals_enabled', True)
    monkeypatch.setattr(cfg, 'referral_reward_credits', 3)
    monkeypatch.setattr(cfg, 'referral_max_rewards', 2)
    monkeypatch.setattr(cfg, 'daily_free_generations', 1)
    return cfg


async def invite(sessions, child, payload=None):
    async with sessions.begin() as s:
        parent, _ = await ensure_profile(s, 10)
        token = parent.invite_token
    async with sessions.begin() as s:
        return await ensure_profile(s, child, payload if payload is not None else f'ref_{token}')


async def test_new_user_first_touch_and_old_user_exclusion(sessions, settings):
    child, new = await invite(sessions, 20)
    assert new and child.inviter_id == 10
    async with sessions.begin() as s:
        other, _ = await ensure_profile(s, 30)
        same, new = await ensure_profile(s, 20, f'ref_{other.invite_token}')
        assert not new and same.inviter_id == 10
        own, _ = await ensure_profile(s, 10, f'ref_{(await s.get(BotProfile,10)).invite_token}')
        assert own.inviter_id is None
        s.add(UserSettings(telegram_user_id=40))
    old, _ = await invite(sessions, 40)
    assert old.inviter_id is None


@pytest.mark.parametrize('payload', ['ref_bad', 'ref_' + 'x'*500, '/start', ''])
async def test_malformed_payload_no_reward(sessions, settings, payload):
    child, _ = await invite(sessions, 20, payload)
    assert child.inviter_id is None


async def test_disabled_referrals_and_reward_snapshot(sessions, settings):
    settings.referrals_enabled = False
    child, _ = await invite(sessions, 20)
    assert child.inviter_id is None
    settings.referrals_enabled = True
    child, _ = await invite(sessions, 21)
    settings.referral_reward_credits = 9
    settings.referrals_enabled = False
    async with sessions.begin() as s:
        assert await qualify_referral(s, 21)
    async with sessions.begin() as s:
        assert await balance(s, 10) == 3


async def test_duplicate_parallel_qualification_and_cap(sessions, settings):
    await invite(sessions, 20)
    async def qualify():
        async with sessions.begin() as s:
            return await qualify_referral(s, 20)
    assert sum(await asyncio.gather(qualify(), qualify())) == 1
    await invite(sessions, 21)
    await invite(sessions, 22)
    async with sessions.begin() as s:
        assert await qualify_referral(s, 21)
        assert not await qualify_referral(s, 22)
    async with sessions.begin() as s:
        assert await balance(s, 10) == 6
        assert (await s.get(BotProfile,10)).rewarded_invites == 2


async def test_success_hook_downloads_do_not_reward(sessions, settings):
    await invite(sessions, 20)
    async with sessions.begin() as s:
        job = Job(telegram_user_id=20, telegram_chat_id=20, source_message_id=1, status=JobStatus.READY)
        s.add(job)
        await s.flush()
        await JobRepository(s).mark_completed(job.id, clips_generated=0)
        assert await balance(s, 10) == 0
        await JobRepository(s).mark_completed(job.id, clips_generated=1)
        await JobRepository(s).mark_completed(job.id, clips_generated=3)
        assert await balance(s, 10) == 3


async def test_allowance_credit_consumption_and_error_refund(sessions, settings):
    async with sessions.begin() as s:
        await reserve(s, 10, 'first', settings)
        job = Job(telegram_user_id=10, telegram_chat_id=10, source_message_id=1, status=JobStatus.READY)
        s.add(job)
        await s.flush()
        token = current_run.set('first')
        try:
            await JobRepository(s).mark_completed(job.id, clips_generated=3)
        finally:
            current_run.reset(token)
        assert not await refund_unfinished(s, 'first')
    async with sessions.begin() as s:
        with pytest.raises(AllowanceRejected):
            await reserve(s, 10, 'second', settings)
        s.add(CreditEntry(key='referral:123', telegram_user_id=10, amount=3))
    async with sessions.begin() as s:
        await reserve(s, 10, 'second', settings)
        assert await balance(s, 10) == 2
        with pytest.raises(AllowanceRejected):
            await reserve(s, 10, 'third', settings)
        assert await refund_unfinished(s, 'second')
        assert not await refund_unfinished(s, 'second')
        assert await balance(s, 10) == 3
    async with sessions.begin() as s:
        with pytest.raises(AllowanceRejected):
            await reserve(s, 10, 'second', settings)


async def test_onboarding_first_start_and_referral_capture(sessions, settings, monkeypatch):
    from app.bot.handlers import start
    from app.database.session import db_manager
    from app.services import current_media
    @asynccontextmanager
    async def session():
        async with sessions.begin() as s:
            yield s
    monkeypatch.setattr(db_manager, 'session', session)
    monkeypatch.setattr(current_media, 'resolve_current_media', AsyncMock(return_value=None))
    async with sessions.begin() as s:
        parent, _ = await ensure_profile(s, 10)
    message = SimpleNamespace(chat=SimpleNamespace(type='private'), from_user=SimpleNamespace(id=20),
        text=f'/start ref_{parent.invite_token}', answer=AsyncMock(), bot=object())
    await start.cmd_start(message)
    assert 'Первый ролик' in message.answer.call_args.args[0]
    async with sessions.begin() as s:
        profile = await s.get(BotProfile, 20)
        assert profile.onboarding_seen and profile.inviter_id == 10
    message.answer.reset_mock()
    await start.cmd_start(message)
    assert 'Что сделать' in message.answer.call_args.args[0]


async def test_onboarding_failed_send_can_retry(sessions, settings, monkeypatch):
    from app.bot.handlers import start
    from app.database.session import db_manager
    @asynccontextmanager
    async def session():
        async with sessions.begin() as s:
            yield s
    monkeypatch.setattr(db_manager, 'session', session)
    message = SimpleNamespace(chat=SimpleNamespace(type='private'), from_user=SimpleNamespace(id=20),
        text='/start', answer=AsyncMock(side_effect=RuntimeError('Telegram unavailable')), bot=object())
    with pytest.raises(RuntimeError):
        await start.cmd_start(message)
    async with sessions.begin() as s:
        assert not (await s.get(BotProfile,20)).onboarding_seen


async def test_concurrent_reservations_cannot_overspend(sessions, settings):
    settings.daily_free_generations = 0
    async with sessions.begin() as s:
        await ensure_profile(s, 10)
        s.add(CreditEntry(key='referral:20', telegram_user_id=10, amount=1))
    async def attempt(key):
        try:
            async with sessions.begin() as s:
                await reserve(s, 10, key, settings)
            return True
        except AllowanceRejected:
            return False
    assert sum(await asyncio.gather(attempt('one'), attempt('two'))) == 1
    async with sessions.begin() as s:
        assert await balance(s, 10) == 0


@pytest.mark.parametrize('success', [False, True])
async def test_middleware_finishes_or_refunds(sessions, settings, monkeypatch, success):
    from app.bot.middlewares.generations import GenerationMiddleware
    from app.database.session import db_manager
    settings.generation_limits_enabled = True
    monkeypatch.setattr(settings, 'generation_limits_enabled', True)
    settings.daily_free_generations = 0
    @asynccontextmanager
    async def session():
        async with sessions.begin() as s:
            yield s
    monkeypatch.setattr(db_manager, 'session', session)
    async with sessions.begin() as s:
        await ensure_profile(s,10)
        s.add(CreditEntry(key='referral:123',telegram_user_id=10,amount=1))
        job=Job(telegram_user_id=10,telegram_chat_id=10,source_message_id=1)
        s.add(job)
        await s.flush()
        job_id=job.id
    async def handler(event, data):
        if success:
            async with sessions.begin() as s:
                await JobRepository(s).mark_completed(job_id,1)
        else:
            raise RuntimeError('render failure')
    event=SimpleNamespace(id='callback1',data='action:maximum_transform',
        from_user=SimpleNamespace(id=10),answer=AsyncMock())
    if success:
        await GenerationMiddleware()(handler,event,{})
    else:
        with pytest.raises(RuntimeError):
            await GenerationMiddleware()(handler,event,{})
    async with sessions.begin() as s:
        assert await balance(s,10)==(0 if success else 1)
        run=await s.scalar(select(GenerationRun))
        assert run.status==('completed' if success else 'refunded')
    assert current_run.get() is None


async def test_disabled_limits_and_testers_bypass_db(settings,monkeypatch):
    from app.bot.middlewares.generations import GenerationMiddleware
    settings.generation_limits_enabled = False
    event=SimpleNamespace(id='x',data='action:maximum_transform',from_user=SimpleNamespace(id=10))
    handler=AsyncMock()
    await GenerationMiddleware()(handler,event,{})
    handler.assert_awaited_once()
    monkeypatch.setattr(settings,'generation_limits_enabled',True)
    monkeypatch.setattr(settings,'unlimited_telegram_ids','10,20')
    handler.reset_mock()
    await GenerationMiddleware()(handler,event,{})
    handler.assert_awaited_once()


async def test_remaining_allowance_tracks_reservations_refunds_and_previous_days(sessions, settings):
    from app.services.generations import free_remaining
    settings.daily_free_generations = 3
    async with sessions.begin() as s:
        assert await free_remaining(s, 10, settings) == 3
        s.add(GenerationRun(request_id='yesterday', telegram_user_id=10, day='2020-01-01',
                            status='completed', uses_credit=False))
        await reserve(s, 10, 'today', settings)
        assert await free_remaining(s, 10, settings) == 2
        await refund_unfinished(s, 'today')
        assert await free_remaining(s, 10, settings) == 3


def test_testers_keep_unlimited_without_exempting_new_users(settings, monkeypatch):
    monkeypatch.setattr(settings, 'generation_limits_enabled', True)
    monkeypatch.setattr(settings, 'allowed_telegram_user_ids', '10,20')
    monkeypatch.setattr(settings, 'unlimited_telegram_ids', '30')
    monkeypatch.setattr(settings, 'beta_testers_unlimited', True)
    assert all(settings.has_unlimited_generations(uid) for uid in (10,20,30))
    assert not settings.has_unlimited_generations(40)
    monkeypatch.setattr(settings, 'beta_testers_unlimited', False)
    assert not settings.has_unlimited_generations(10)
    assert settings.has_unlimited_generations(30)
