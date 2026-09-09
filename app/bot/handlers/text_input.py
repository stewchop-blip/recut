"""Text message handler — validates input, shows voice keyboard."""

from aiogram import F, Router, types
from aiogram.filters import Command

from app.core.config import get_settings
from app.core.limits import LimitsManager, get_limits_manager
from app.bot.keyboards.inline import get_voice_keyboard
from app.core.logging import get_logger

router = Router()
logger = get_logger(__name__)


@router.message(F.text)
async def handle_text(message: types.Message) -> None:
    text = message.text or ""
    settings = get_settings()
    limits = get_limits_manager()
    user_id = message.from_user.id if message.from_user else 0

    # Validate length
    check = limits.check_text_length(text)
    if not check.allowed:
        await message.answer(
            f"❌ {check.reason}\n\nПопробуй короче или разбей текст на части."
        )
        return

    # Check quota
    quota = limits.check_daily_generations(user_id)
    if not quota.allowed:
        await message.answer(
            f"❌ {quota.reason}\nОсталось: {quota.current_count}/{quota.limit}"
        )
        return

    # Save to temporary state (using message_id + user_id as job key)
    # For MVP: just show voice selection — no persistent state yet
    await message.answer(
        f"✅ Текст получен ({len(text)} симв.).\n\nВыбери голос:",
        reply_markup=get_voice_keyboard(),
    )

    logger.info(
        "text_input_accepted",
        telegram_user_id=user_id,
        text_length=len(text),
        quota_current=quota.current_count,
    )