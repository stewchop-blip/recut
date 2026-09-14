"""/start and /help handlers — friendly, minimal, no marketing fluff."""
from aiogram import F, Router, types
from aiogram.filters import Command

from app.bot.keyboards.inline import START_KEYBOARD

router = Router()


WELCOME = (
    "👋 Привет! Я <b>Recut</b> — озвучка текста для видео.\n\n"
    "<b>Как пользоваться:</b>\n"
    "1️⃣ Отправь мне текст (до 4000 символов)\n"
    "2️⃣ Выбери голос\n"
    "3️⃣ Получи аудиофайл\n\n"
    "🎙 Голоса: мужской, женский, энергичный\n"
    "📊 Лимит: 20 озвучек в день\n\n"
    "<i>Просто отправь любой текст — и начнём.</i>"
)


HELP_TEXT = (
    "📖 <b>Что я умею</b>\n\n"
    "• Озвучиваю текст тремя голосами (мужской / женский / энергичный)\n"
    "• Возвращаю WAV-файл, готовый для монтажа\n"
    "• Лимит: 20 генераций в сутки на пользователя\n\n"
    "<b>Что я НЕ умею (пока):</b>\n"
    "• Не генерирую музыку\n"
    "• Не редактирую текст автоматически\n"
    "• Не делаю видео\n\n"
    "Вопросы → @stewchop"
)


@router.message(Command("start"))
async def cmd_start(message: types.Message) -> None:
    await message.answer(WELCOME, reply_markup=START_KEYBOARD, parse_mode="HTML")


@router.message(Command("help"))
async def cmd_help(message: types.Message) -> None:
    await message.answer(HELP_TEXT, parse_mode="HTML")


@router.callback_query(F.data == "start:help")
async def on_help_click(call: types.CallbackQuery) -> None:
    await call.message.edit_text(HELP_TEXT, parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data == "action:change_voice")
async def on_change_voice(call: types.CallbackQuery) -> None:
    """User wants the same text in a different voice.
    Re-show the voice keyboard — the text is already in UserTextStore."""
    from app.bot.keyboards.inline import get_voice_keyboard
    from app.bot.user_text_store import get_user_text_store

    user_id = call.from_user.id if call.from_user else 0
    pending = await get_user_text_store().get(user_id)
    if pending is None:
        await call.answer("⚠️ Сначала отправь текст.", show_alert=True)
        return
    await call.message.answer(
        f"🎙 Выбери другой голос для:\n\n«{pending.text[:200]}{'…' if len(pending.text) > 200 else ''}»",
        reply_markup=get_voice_keyboard(),
    )
    await call.answer()