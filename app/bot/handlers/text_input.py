"""Text message handler — validates input, shows voice keyboard, stores text."""
from aiogram import F, Router, types

from app.bot.keyboards.inline import get_voice_keyboard
from app.bot.user_text_store import get_user_text_store
from app.core.limits import get_limits_manager
from app.core.logging import get_logger

router = Router()
logger = get_logger(__name__)


@router.message(F.text)
async def handle_text(message: types.Message) -> None:
    text = (message.text or "").strip()
    user_id = message.from_user.id if message.from_user else 0
    limits = get_limits_manager()

    # Ignore commands — they are handled by other routers
    if text.startswith("/"):
        return

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

    # Persist text so the voice callback can find it
    await get_user_text_store().put(user_id, text, message.message_id)

    # Show voice selection
    await message.answer(
        f"✅ Текст принят ({len(text)} симв.)\n\nВыбери голос 👇",
        reply_markup=get_voice_keyboard(),
    )

    logger.info(
        "text_input_accepted",
        telegram_user_id=user_id,
        text_length=len(text),
        quota_current=quota.current_count,
    )