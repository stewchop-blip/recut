"""Private operator-only acquisition reports and campaign links."""
from aiogram import Router, types
from aiogram.filters import Command
from aiogram.utils.deep_linking import create_start_link

from app.core.config import get_settings
from app.core.logging import get_logger
from app.database.session import db_manager
from app.services.analytics import DEFAULT_SOURCES, SOURCE_SLUG, report, format_report

router = Router()
logger = get_logger(__name__)


def _authorized(message):
    return (message.chat.type == 'private' and message.from_user
            and message.from_user.id in get_settings().analytics_admin_ids)


@router.message(Command('stats'))
async def cmd_stats(message: types.Message):
    if not _authorized(message):
        return
    parts = (message.text or '').split()
    if (len(parts) > 3 or (len(parts) > 1 and parts[1] not in {'7', '30', '90'})
            or (len(parts) > 2 and not SOURCE_SLUG.fullmatch(parts[2]))):
        await message.answer('Примеры: /stats · /stats 7 · /stats 30 tiktok', parse_mode=None)
        return
    days = int(parts[1]) if len(parts) > 1 else 30
    source = parts[2] if len(parts) > 2 else None
    try:
        async with db_manager.session() as session:
            result = await report(session, days, source)
        await message.answer(format_report(result), parse_mode=None)
    except Exception:
        logger.exception('analytics_report_failed')
        await message.answer('Не удалось получить статистику. Попробуй позже.', parse_mode=None)


@router.message(Command('links'))
async def cmd_links(message: types.Message):
    if not _authorized(message):
        return
    parts = (message.text or '').split()
    if len(parts) > 2 or (len(parts) == 2 and not SOURCE_SLUG.fullmatch(parts[1])):
        await message.answer('Пример: /links tiktok_video_01. '
            'Метка: до 48 латинских строчных букв, цифр, дефисов и подчёркиваний.', parse_mode=None)
        return
    sources = (parts[1],) if len(parts) == 2 else DEFAULT_SOURCES
    lines = ['🔗 Ссылки для размещения', '']
    for source in sources:
        lines += [source, await create_start_link(message.bot, f'src_{source}'), '']
    lines += ['Своя метка: /links tiktok_video_01',
              'Результаты: /stats 30 tiktok_video_01',
              'Метка показывает, какую ссылку использовали при первом контакте. '
              'Пересланная ссылка сохранит ту же метку. Это не реферальный бонус.',
              'Пока бот закрыт, внешние посетители не смогут пройти дальше ограничения доступа.']
    await message.answer('\n'.join(lines), parse_mode=None, disable_web_page_preview=True)
