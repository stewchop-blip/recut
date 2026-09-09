"""Text preparation (rewrite) flow handlers."""

from aiogram import F, Router
from aiogram.types import CallbackQuery

from app.services.text.rewriter import get_text_rewriter, RewriteError
from app.bot.keyboards.inline import get_rewrite_keyboard
from app.core.logging import get_logger

router = Router()
logger = get_logger(__name__)


@router.callback_query(F.data == "action:rewrite")
async def on_rewrite_request(call: CallbackQuery) -> None:
    await call.answer("✏️ Готовлю текст...")
    # Get text from context (simplified — in full version retrieve from DB/state)
    text = "Текст для подготовки к озвучке."

    rewriter = get_text_rewriter()
    try:
        rewritten = await rewriter.rewrite(text)
        await call.message.answer(
            f"✏️ Готово:\n\n{rewritten}\n\nВыбери действие:",
            reply_markup=get_rewrite_keyboard(),
        )
    except RewriteError as e:
        logger.error("rewrite_failed", error=str(e))
        await call.message.answer("❌ Ошибка переписывания. Попробуй ещё раз.")
    finally:
        await rewriter.close()


@router.callback_query(F.data == "rewrite:voice")
async def on_rewrite_voice(call: CallbackQuery) -> None:
    await call.answer("🎙 Озвучиваю...")
    await call.message.answer("Отправь голос (через /start или текст + голос) — пока покажу клавиатуру:")
    # In full flow: use rewritten text, call TTS


@router.callback_query(F.data == "rewrite:original")
async def on_rewrite_use_original(call: CallbackQuery) -> None:
    await call.answer("✓ Используй оригинал")
    await call.message.answer("Оригинальный текст сохранён.")


@router.callback_query(F.data == "rewrite:again")
async def on_rewrite_again(call: CallbackQuery) -> None:
    await on_rewrite_request(call)