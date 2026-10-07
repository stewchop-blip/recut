"""/start and /help handlers for the Quick Prep UX."""
from pathlib import Path

from aiogram import F, Router, types
from aiogram.filters import Command

from app.bot.notices import INSTAGRAM_MAINTENANCE
from app.core.logging import get_logger
from app.database.repositories import JobRepository
from app.database.session import db_manager

router = Router()
logger = get_logger(__name__)

BOT_DESCRIPTION = (
    "Скинь ссылку на TikTok или YouTube Shorts — я загружу видео сюда.\n\n"
    "Потом сможешь обработать его в пару нажатий.\n\n"
    "Нажми «Начать», чтобы прислать ссылку или своё видео."
)


async def setup_bot_description(bot) -> None:
    """Explain the product before Start, including Telegram's Russian override."""
    for language in ("", "ru"):
        try:
            await bot.set_my_description(description=BOT_DESCRIPTION, language_code=language)
        except Exception:
            logger.exception("telegram_description_failed", language=language)


WELCOME = (
    "👋 <b>Скинь ссылку на TikTok или YouTube Shorts</b>\n"
    "Я загружу видео сюда.\n\n"
    "Потом сможешь обработать его в пару нажатий.\n\n"
    "👇 Просто вставь ссылку\n\n"
    + INSTAGRAM_MAINTENANCE
)

START_MENU = types.InlineKeyboardMarkup(inline_keyboard=[
    [types.InlineKeyboardButton(text="📎 Вставить ссылку", callback_data="intake:link")],
    [types.InlineKeyboardButton(text="📤 Загрузить видео", callback_data="intake:video")],
])


@router.callback_query(F.data.in_({"intake:link", "intake:video"}))
async def intake_prompt(call: types.CallbackQuery) -> None:
    await call.answer()
    if not call.message:
        return
    text = (
        "📎 Скопируй ссылку на видео в TikTok или YouTube "
        "и вставь её в сообщение сюда.\n\n" + INSTAGRAM_MAINTENANCE
        if call.data == "intake:link" else
        "📤 Нажми скрепку рядом с полем сообщения, выбери видео и отправь его сюда."
    )
    await call.message.answer(text, reply_markup=types.ForceReply(
        selective=True, input_field_placeholder="Вставь ссылку на видео" if call.data == "intake:link"
        else "Прикрепи видео через скрепку"))


@router.message(Command("menu"))
async def cmd_menu(message: types.Message) -> None:
    from app.bot.keyboards.inline import HOME_MENU
    await message.answer("Что сделать с видео?", reply_markup=HOME_MENU)


HELP_TEXT = (
    "📖 <b>Как пользоваться ReCut</b>\n\n"
    "<b>Короткое видео:</b> отправь файл или ссылку, затем нажми «Сделать». "
    "В «Ещё» доступны три варианта и максимальная обработка со сменой цвета, скорости и звука.\n\n"
    "<b>Оформление:</b> выбери стиль, загрузи свою плашку (баннер) или включи персонажа снизу. Плашка — это картинка или анимация поверх видео. "
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
        from app.bot.handlers.video import reset_media_selection
        await reset_media_selection(user_id)
        if first_visit:
            await message.answer(WELCOME, parse_mode="HTML", reply_markup=START_MENU)
            async with db_manager.session() as session:
                await session.execute(update(BotProfile).where(
                    BotProfile.telegram_user_id == user_id).values(onboarding_seen=True))
            return
    except Exception:
        logger.exception("onboarding_failed", user_id=user_id)
        await message.answer("Не удалось начать заново. Попробуй /start ещё раз.")
        return
    await message.answer(
        WELCOME,
        parse_mode="HTML",
        reply_markup=START_MENU,
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
        from app.bot.handlers.video import reset_media_selection
        await reset_media_selection(user_id)
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
            "✅ Выбор видео сброшен.\n"
            "Отправь новое видео или ссылку."
        )
