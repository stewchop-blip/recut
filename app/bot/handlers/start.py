"""/start and /help handlers for the Quick Prep UX."""
from aiogram import Router, types
from aiogram.filters import Command

from app.core.logging import get_logger
from app.database.repositories import JobRepository
from app.database.session import db_manager

router = Router()
logger = get_logger(__name__)


WELCOME = (
    "\U0001f3ac <b>ReCut</b>\n\n"
    "\u041f\u0440\u0438\u0448\u043b\u0438 \u0432\u0438\u0434\u0435\u043e \u0438\u043b\u0438 \u0441\u0441\u044b\u043b\u043a\u0443 "
    "\u043d\u0430 TikTok, Reels \u0438\u043b\u0438 Shorts.\n\n"
    "\u042f \u043f\u043e\u0434\u0433\u043e\u0442\u043e\u0432\u043b\u044e \u0440\u043e\u043b\u0438\u043a:\n"
    "\u2022 \u043f\u0440\u0438\u0432\u0435\u0434\u0443 \u043a \u0443\u0434\u043e\u0431\u043d\u043e\u043c\u0443 \u0444\u043e\u0440\u043c\u0430\u0442\u0443\n"
    "\u2022 \u0434\u043e\u0431\u0430\u0432\u043b\u044e \u043f\u043b\u0430\u0448\u043a\u0443\n"
    "\u2022 \u043f\u0440\u0438 \u043d\u0435\u043e\u0431\u0445\u043e\u0434\u0438\u043c\u043e\u0441\u0442\u0438 \u0441\u0434\u0435\u043b\u0430\u044e "
    "\u0441\u0443\u0431\u0442\u0438\u0442\u0440\u044b\n\n"
    "\u0414\u043b\u044f \u0434\u043b\u0438\u043d\u043d\u043e\u0433\u043e \u0432\u0438\u0434\u0435\u043e \u043c\u043e\u0433\u0443 "
    "\u043d\u0430\u0439\u0442\u0438 \u043e\u0442\u0434\u0435\u043b\u044c\u043d\u044b\u0435 \u043c\u043e\u043c\u0435\u043d\u0442\u044b."
)


HELP_TEXT = (
    "📖 <b>Как пользоваться</b>\n\n"
    "1️⃣ Отправь видео (файл)\n"
    "2️⃣ Нажми «🚀 Подготовить видео»\n"
    "3️⃣ Получи готовый MP4\n\n"
    "⚙️ В настройках: позиция CTA, время показа, баннер, субтитры.\n\n"
    "Вопросы → @stewchop"
)


@router.message(Command("start"))
async def cmd_start(message: types.Message) -> None:
    # If the user has a pending job in memory, restore the action menu
    # instead of showing the welcome text (better UX — the menu is
    # otherwise lost after the status message scrolls away).
    try:
        from app.bot.handlers.video import _pending_jobs
        from app.bot.keyboards.inline import ACTION_MENU
        from pathlib import Path
        user_id = message.from_user.id if message.from_user else 0
        pending = _pending_jobs.get(user_id)
        if pending is not None:
            input_path = Path(pending.input_path)
            if input_path.exists():
                actual_size = input_path.stat().st_size
                await message.answer(
                    f"✅ Видео загружено ({actual_size // 1024 // 1024} МБ).\n\n"
                    f"Выбери действие:",
                    reply_markup=ACTION_MENU,
                )
                return
    except Exception:
        pass
    await message.answer(WELCOME, parse_mode="HTML")


@router.message(Command("help"))
async def cmd_help(message: types.Message) -> None:
    await message.answer(HELP_TEXT, parse_mode="HTML")


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
            n = await repo.cancel_active_jobs(user_id, max_age_minutes=10)
    except Exception as e:
        logger.error("cancel_db_failed", user_id=user_id, error=str(e)[:200])
        await message.answer("❌ Не удалось отменить задачу. Попробуй ещё раз.")
        return

    # Best-effort cleanup of any leftover job workspaces.
    import shutil
    from pathlib import Path
    base = Path("/tmp/recut")
    if base.exists():
        for entry in base.iterdir():
            try:
                # only remove directories that look like job dirs and aren't
                # currently active (avoid racing with another in-flight job).
                if entry.is_dir() and entry.name.startswith("job_"):
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