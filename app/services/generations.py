"""Optional daily allowance + credit reservations shared by all render actions."""
from contextvars import ContextVar
from datetime import datetime, timezone

from sqlalchemy import select, func, update

from app.database.models import BotProfile, CreditEntry, GenerationRun
from app.services.payments import balance
from app.services.referrals import ensure_profile

current_run: ContextVar[str | None] = ContextVar('generation_run', default=None)


class AllowanceRejected(ValueError):
    pass


async def reserve(session, user_id, request_id, settings):
    await ensure_profile(session, user_id)
    # Serialize reservations per user across replicas (PostgreSQL row lock).
    await session.execute(update(BotProfile).where(
        BotProfile.telegram_user_id == user_id).values(
            onboarding_seen=BotProfile.onboarding_seen))
    if await session.get(GenerationRun, request_id):
        raise AllowanceRejected('Эта команда уже обработана. Открой действия для видео заново.')
    active = await session.scalar(select(GenerationRun.request_id).where(
        GenerationRun.telegram_user_id == user_id, GenerationRun.status == 'reserved').limit(1))
    if active:
        raise AllowanceRejected('Предыдущая обработка ещё выполняется. Дождись результата.')
    day = datetime.now(timezone.utc).date().isoformat()
    used = await session.scalar(select(func.count()).select_from(GenerationRun).where(
        GenerationRun.telegram_user_id == user_id, GenerationRun.day == day,
        GenerationRun.uses_credit.is_(False), GenerationRun.status.in_(['reserved', 'completed'])))
    uses_credit = int(used or 0) >= settings.daily_free_generations
    if uses_credit and await balance(session, user_id) <= 0:
        raise AllowanceRejected('Бесплатные обработки на сегодня закончились. '
            'Новый лимит — в 03:00 по Минску. Бонусы за друзей: /referral')
    session.add(GenerationRun(request_id=request_id, telegram_user_id=user_id,
        day=day, uses_credit=uses_credit, status='reserved'))
    if uses_credit:
        session.add(CreditEntry(key=f'usage:{request_id}', telegram_user_id=user_id, amount=-1))
    await session.flush()


async def complete_current_run(session, user_id):
    run_id = current_run.get()
    if run_id:
        await session.execute(update(GenerationRun).where(
            GenerationRun.request_id == run_id, GenerationRun.telegram_user_id == user_id,
            GenerationRun.status == 'reserved').values(status='completed'))


async def refund_unfinished(session, request_id):
    run = await session.get(GenerationRun, request_id)
    if run is None or run.status != 'reserved':
        return False
    claimed = await session.execute(update(GenerationRun).where(
        GenerationRun.request_id == request_id, GenerationRun.status == 'reserved'
    ).values(status='refunded'))
    if claimed.rowcount != 1:
        return False
    if run.uses_credit:
        session.add(CreditEntry(key=f'usage_refund:{request_id}',
            telegram_user_id=run.telegram_user_id, amount=1))
    await session.flush()
    return True
