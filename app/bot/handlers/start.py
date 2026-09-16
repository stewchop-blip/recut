"""/start and /help handlers for the video-repurpose bot."""
from aiogram import Router, types
from aiogram.filters import Command

from app.bot.keyboards.inline import start_keyboard

router = Router()


WELCOME = (
    "🎬 <b>Recut</b>\n\n"
    "Отправь видео — я найду интересные моменты и подготовлю короткие ролики.\n\n"
    "📹 Поддержка: горизонтальное и вертикальное видео с речью\n"
    "⏱ Длительность: до 60 минут\n"
    "📦 Размер: до 1.5 ГБ\n\n"
    "<i>На этапе тестирования бот работает в закрытом режиме.</i>"
)


HELP_TEXT = (
    "📖 <b>Как пользоваться</b>\n\n"
    "1️⃣ Отправь видео (файл или кружок)\n"
    "2️⃣ Бот распознает речь\n"
    "3️⃣ Найдёт 3 самостоятельных момента\n"
    "4️⃣ Нарежет клипы\n"
    "5️⃣ Добавит субтитры\n"
    "6️⃣ Вернёт готовые MP4\n\n"
    "Вопросы → @stewchop"
)


@router.message(Command("start"))
async def cmd_start(message: types.Message) -> None:
    await message.answer(WELCOME, reply_markup=start_keyboard(), parse_mode="HTML")


@router.message(Command("help"))
async def cmd_help(message: types.Message) -> None:
    await message.answer(HELP_TEXT, parse_mode="HTML")


@router.message(Command("cancel"))
async def cmd_cancel(message: types.Message) -> None:
    await message.answer("❌ Ок. Отправь видео, когда будешь готов.")
