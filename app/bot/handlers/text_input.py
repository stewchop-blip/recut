"""Text message handler — validates input, shows voice keyboard, stores text in FSM."""
from aiogram import F, Router, types
from aiogram.fsm.context import FSMContext

from app.core.config import get_settings
from app.core.limits import LimitsManager, get_limits_manager
from app.bot.keyboards.inline import get_voice_keyboard
from app.bot.states import RecutStates
from app.core.logging import get_logger

router = Router()
logger = get_logger(__name__)


@router.message(F.text)
async def handle_text(message: types.Message, state: FSMContext) -> None:
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
        await state.clear()
        return

    # Check quota
    quota = limits.check_daily_generations(user_id)
    if not quota.allowed:
        await message.answer(
            f"❌ {quota.reason}\nОсталось: {quota.current_count}/{quota.limit}"
        )
        await state.clear()
        return

    # Persist the actual user text + message_id so the voice callback can find it
    await state.set_state(RecutStates.awaiting_voice)
    await state.update_data(
        text=text,
        prompt_message_id=message.message_id,
        prompt_chat_id=message.chat.id,
    )

    # Show voice selection
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