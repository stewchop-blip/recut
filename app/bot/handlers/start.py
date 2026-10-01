"""/start and /help handlers for the Quick Prep UX."""
from pathlib import Path

from aiogram import F, Router, types
from aiogram.filters import Command

from app.core.logging import get_logger
from app.database.repositories import JobRepository
from app.database.session import db_manager

router = Router()
logger = get_logger(__name__)


WELCOME = (
    "🎬 <b>ReCut — подготовка видео для публикации</b>\n\n"
    "Отправь видео файлом или ссылку на TikTok, Reels или Shorts.\n\n"
    "• Скачаю исходник по ссылке.\n"
    "• Сделаю вертикальный ролик с оформлением, твоей плашкой и персонажем.\n"
    "• Создам три монтажных варианта или найду моменты в длинном видео.\n\n"
    "<b>Первый ролик — в три шага:</b>\n"
    "1. Пришли видео или ссылку.\n"
    "2. Нажми «Сделать».\n"
    "3. Получи готовый MP4.\n\n"
    "Плашку и персонажа можно включить в «Оформлении». "
    "Максимальная обработка находится в «Ещё»."
)

HELP_TEXT = (
    "📖 <b>Как пользоваться ReCut</b>\n\n"
    "<b>Короткое видео:</b> отправь файл или ссылку, затем нажми «Сделать». "
    "В «Ещё» доступны три варианта и максимальная обработка со сменой цвета, скорости и звука.\n\n"
    "<b>Оформление:</b> выбери стиль, загрузи свою плашку или включи персонажа снизу. "
    "Настройки сохраняются для следующих роликов.\n\n"
    "<b>Длинное видео:</b> выбери «Нарезать длинное видео» и отправь источник. "
    "Бот подберёт моменты; субтитры доступны в обработке длинных видео.\n\n"
    "Скачать исходник можно без обработки. Изменение оформления не гарантирует "
    "попадания в рекомендации площадок.\n\n"
    "/referral — приглашения и бонусы\n/balance — баланс обработок\n"
    "/cancel — отменить зависшую задачу\nПоддержка: @stewchop"
)


@router.message(Command("start"))
async def cmd_start(message: types.Message) -> None:
    from app.bot.keyboards.inline import HOME_MENU
    if message.chat.type != "private" or not message.from_user:
        return
    from sqlalchemy import update
    from app.database.models import BotProfile
    from app.services.referrals import ensure_profile
    user_id = message.from_user.id
    payload = (message.text or "").split(maxsplit=1)
    payload = payload[1].strip() if len(payload) > 1 else ""
    try:
        async with db_manager.session() as session:
            profile, _ = await ensure_profile(session, user_id, payload)
            first_visit = not profile.onboarding_seen
        if first_visit:
            await message.answer(WELCOME, parse_mode="HTML", reply_markup=HOME_MENU)
            async with db_manager.session() as session:
                await session.execute(update(BotProfile).where(
                    BotProfile.telegram_user_id == user_id).values(onboarding_seen=True))
            return
    except Exception:
        logger.exception("onboarding_failed", user_id=user_id)
        await message.answer(WELCOME, parse_mode="HTML", reply_markup=HOME_MENU)
        return
    # Item 11: CurrentMedia is the source of truth, not _pending_jobs.
    # If media is recoverable → HOME with "Продолжить" info.
    from app.services.current_media import resolve_current_media
    user_id = message.from_user.id if message.from_user else 0
    try:
        cm = await resolve_current_media(user_id, message.bot)
    except Exception:
        cm = None
    if cm is not None and cm.source_path and cm.source_path.is_file():
        from app.bot.keyboards.inline import ACTION_MENU
        await message.answer(
            "🎬 <b>ReCut</b>\n\nТекущее видео готово. Продолжаем?",
            parse_mode="HTML",
            reply_markup=ACTION_MENU,
        )
        return
    await message.answer(
        "🎬 <b>ReCut</b>\n\nЧто сделать?",
        parse_mode="HTML",
        reply_markup=HOME_MENU,
    )


@router.message(Command("help"))
async def cmd_help(message: types.Message) -> None:
    from app.bot.keyboards.inline import HOME_MENU
    await message.answer(HELP_TEXT, parse_mode="HTML", reply_markup=HOME_MENU)


@router.callback_query(F.data == "help:show")
async def help_callback(call: types.CallbackQuery) -> None:
    await call.answer()
    if call.message:
        await call.message.answer(HELP_TEXT, parse_mode="HTML")


@router.message(Command("cancel"))
async def cmd_cancel(message: types.Message) -> None:
    """Cancel any in-flight Job for this user.

    Marks active jobs as CANCELLED so has_active_job() stops blocking
    new videos. Also deletes the corresponding /tmp/recut/* workdir.
    """
    user_id = message.from_user.id if message.from_user else 0
    if not user_id:
        return

    try:
        async with db_manager.session() as session:
            repo = JobRepository(session)
            n, cancelled_ids = await repo.cancel_active_jobs(user_id, max_age_minutes=10)
    except Exception as e:
        logger.error("cancel_db_failed", user_id=user_id, error=str(e)[:200])
        await message.answer("❌ Не удалось отменить задачу. Попробуй ещё раз.")
        return

    # Phase 6 fix: only clean directories for this user's cancelled jobs,
    # never delete other users' workspace.
    import shutil
    from app.core.config import get_settings
    base = Path(get_settings().temp_dir)
    if base.exists() and cancelled_ids:
        for entry in base.iterdir():
            try:
                if entry.is_dir() and entry.name.startswith("job_"):
                    # Only delete if directory references one of our cancelled job IDs
                    if any(entry.name == f"job_{job_id}" or entry.name.startswith(f"job_{job_id}_")
                           for job_id in cancelled_ids):
                        shutil.rmtree(entry, ignore_errors=True)
            except Exception:
                pass

    if n:
        await message.answer(
            f"✅ Отменил {n} задач(у).\n"
            "Можешь отправлять новое видео."
        )
    else:
        await message.answer(
            "ℹ️ Не было активных задач.\n"
            "Можешь отправлять видео когда будешь готов."
        )