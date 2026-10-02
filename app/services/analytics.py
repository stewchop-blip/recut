"""First-touch acquisition, activation and mature calendar-day retention (UTC)."""
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update, func, case, exists
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.core.config import get_settings
from app.database.models import Acquisition, ActivityDay, BotProfile, Job, User, UserSettings

SOURCE_SLUG = re.compile(r'[a-z0-9][a-z0-9_-]{0,47}\Z')
DEFAULT_SOURCES = ('tiktok', 'instagram', 'youtube', 'telegram')


def source_from_payload(payload):
    if payload.startswith('src_') and SOURCE_SLUG.fullmatch(payload[4:]):
        return payload[4:]
    if re.fullmatch(r'ref_[A-Za-z0-9_-]{24}', payload):
        return 'referral'
    return 'direct'


def _insert(session, model):
    return (sqlite_insert if session.bind.dialect.name == 'sqlite' else pg_insert)(model)


async def observe(session, user_id, payload='', *, now=None):
    now = now or datetime.now(timezone.utc)
    existing = await session.get(Acquisition, user_id)
    if existing is None:
        # Legacy data is deliberately NOT backfilled with an invented arrival date/source.
        legacy = False
        for model in (BotProfile, User, UserSettings, Job):
            if await session.scalar(select(model.telegram_user_id).where(
                    model.telegram_user_id == user_id).limit(1)) is not None:
                legacy = True
                break
        await session.execute(_insert(session, Acquisition).values(
            telegram_user_id=user_id, source=source_from_payload(payload), is_new=not legacy,
            is_internal=user_id in get_settings().internal_user_ids, first_seen_at=now,
            d1_day=(now.date()+timedelta(days=1)).isoformat(),
            d7_day=(now.date()+timedelta(days=7)).isoformat(),
        ).on_conflict_do_nothing(index_elements=['telegram_user_id']))
    await session.execute(_insert(session, ActivityDay).values(
        telegram_user_id=user_id, day=now.date().isoformat()
    ).on_conflict_do_nothing(index_elements=['telegram_user_id', 'day']))


async def record_result(session, user_id, *, now=None):
    # A completed download (clips_generated=0) never calls this hook.
    await session.execute(update(Acquisition).where(
        Acquisition.telegram_user_id == user_id, Acquisition.first_result_at.is_(None)
    ).values(first_result_at=now or datetime.now(timezone.utc)))


async def report(session, days=30, source=None, *, now=None):
    now = now or datetime.now(timezone.utc)
    today = now.date().isoformat()
    since = datetime.combine(now.date()-timedelta(days=days-1), datetime.min.time(), timezone.utc)
    a = Acquisition
    filters = [a.is_internal.is_(False)]
    # Also exclude operators configured after their first visit.
    internal = get_settings().internal_user_ids
    if internal:
        filters.append(a.telegram_user_id.not_in(internal))
    if source:
        filters.append(a.source == source)
    cohort = filters + [a.is_new.is_(True), a.first_seen_at >= since, a.first_seen_at <= now]

    def retention(day_column):
        eligible = day_column < today  # only fully elapsed return days
        returned = exists(select(ActivityDay.telegram_user_id).where(
            ActivityDay.telegram_user_id == a.telegram_user_id, ActivityDay.day == day_column))
        return (func.sum(case((eligible, 1), else_=0)),
                func.sum(case((eligible & returned, 1), else_=0)))

    d1_eligible, d1_returned = retention(a.d1_day)
    d7_eligible, d7_returned = retention(a.d7_day)
    measures = [func.count(), func.sum(case((a.first_result_at.is_not(None), 1), else_=0)),
                d1_eligible, d1_returned, d7_eligible, d7_returned]
    total = (await session.execute(select(*measures).where(*cohort))).one()
    # Bound Telegram output; a filtered report can inspect any named source.
    sources = (await session.execute(select(a.source, *measures).where(*cohort)
        .group_by(a.source).order_by(func.count().desc(), a.source).limit(20))).all()
    active = await session.scalar(select(func.count(func.distinct(ActivityDay.telegram_user_id)))
        .join(a, a.telegram_user_id == ActivityDay.telegram_user_id)
        .where(*filters, ActivityDay.day >= since.date().isoformat(), ActivityDay.day <= today))
    tracking_since = await session.scalar(select(func.min(a.first_seen_at)))
    return {'days': days, 'since': since.date().isoformat(), 'today': today,
            'total': tuple(int(x or 0) for x in total), 'sources': sources,
            'active': int(active or 0), 'tracking_since': tracking_since, 'source': source}


def _ratio(numerator, denominator):
    return (f'{numerator}/{denominator} ({numerator/denominator:.0%})'
            if denominator else 'ещё нет данных')


def format_report(data):
    new, activated, d1n, d1r, d7n, d7r = data['total']
    lines = [f"📊 ReCut · {data['since']} — {data['today']} (UTC)"]
    if data['source']:
        lines.append(f"Источник: {data['source']}")
    lines += [f'Новых пользователей: {new}',
              f'Получили обработанное видео: {_ratio(activated, new)}',
              f"Активных за период, включая прежних: {data['active']}",
              f'Вернулись на следующий день, D1: {_ratio(d1r, d1n)}',
              f'Вернулись на 7-й день, D7: {_ratio(d7r, d7n)}', '',
              'Источники · новые / получили видео:']
    for row in data['sources']:
        src, count, done, _, _, _, _ = row
        lines.append(f'{src}: {count} / {done or 0}')
    if not data['sources']:
        lines.append('Пока нет новых пользователей за период.')
    lines += ['', 'Показаны до 20 источников. Подробнее: /stats 30 tiktok',
              'Новые — первый контакт с ботом; источник закрепляется один раз.',
              'Первое видео учитывается к моменту отчёта. D1/D7 — действие в личном чате '
              'в указанный день; только завершившиеся дни. Тестеры исключены.',
              'Это запуски бота, не клики. Историю до включения учёта не восстанавливаем.']
    if data['tracking_since']:
        lines.append(f"Учёт с {data['tracking_since'].strftime('%Y-%m-%d %H:%M')} UTC.")
    return '\n'.join(lines)
