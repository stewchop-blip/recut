"""/start and /help handlers for the Quick Prep UX."""
from aiogram import Router, types
from aiogram.filters import Command

router = Router()


WELCOME = (
    "🎬 <b>Recut</b>\n\n"
    "Отправь видео — подготовлю его к публикации.\n\n"
    "📐 9:16 (1080×1920), с твоим CTA-баннером\n"
    "📏 Лимит Telegram Bot API: 20 МБ\n\n"
    "<i>После первой настройки один тап = готовое видео.</i>"
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
    await message.answer(WELCOME, parse_mode="HTML")


@router.message(Command("help"))
async def cmd_help(message: types.Message) -> None:
    await message.answer(HELP_TEXT, parse_mode="HTML")


@router.message(Command("cancel"))
async def cmd_cancel(message: types.Message) -> None:
    await message.answer("❌ Ок. Отправь видео, когда будешь готов.")