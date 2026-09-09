"""/start and /help handlers."""

from aiogram import Router, types
from aiogram.filters import Command

router = Router()


@router.message(Command("start"))
async def cmd_start(message: types.Message) -> None:
    text = (
        "🎬 Recut — озвучка текста для видео (TikTok, Reels, Shorts).\n\n"
        "Отправь текст → выбери голос → получи аудио.\n\n"
        "Команды:\n"
        "/start — начало\n"
        "/help — помощь\n\n"
        "Можешь отправить текст сразу — бот покажет выбор голоса."
    )
    await message.answer(text, parse_mode="HTML")


@router.message(Command("help"))
async def cmd_help(message: types.Message) -> None:
    text = (
        "📖 Как пользоваться:\n\n"
        "1. Отправь текст (до 4000 символов)\n"
        "2. Выбери голос: 🎙 Мужской / Женский / ⚡ Энергичный\n"
        "3. Получи аудио в ответ\n\n"
        "✨ Подготовить текст для озвучки — улучшает текст через ИИ.\n\n"
        "Лимит: 20 генераций в день."
    )
    await message.answer(text, parse_mode="HTML")


@router.message(Command("cancel"))
async def cmd_cancel(message: types.Message) -> None:
    await message.answer("❌ Отменено. Отправь новый текст или /start.")