"""Callback for voice selection — triggers TTS synthesis."""

import uuid
from aiogram import F, Router, types
from aiogram.types import CallbackQuery

from app.core.config import get_settings
from app.core.limits import get_limits_manager
from app.core.logging import get_logger
from app.services.tts import TTSService
from app.services.media.ffmpeg import get_media_service
from app.utils.temp import get_temp_manager
from app.bot.keyboards.inline import get_result_keyboard

router = Router()
logger = get_logger(__name__)


@router.callback_query(F.data.startswith("voice:"))
async def on_voice_select(call: CallbackQuery) -> None:
    voice = call.data.split(":", 1)[1]
    user_id = call.from_user.id if call.from_user else 0
    job_id = str(uuid.uuid4())

    await call.answer(f"Генерирую голос: {voice}")
    # Use message text for synthesis
    text = call.message.text or "Привет! Тестовое озвучивание."

    limits = get_limits_manager()
    quota = limits.check_daily_generations(user_id)
    if not quota.allowed:
        await call.message.answer("❌ Лимит исчерпан.")
        return

    # Idempotency check
    idempotency_key = f"voice:{user_id}:{call.message.message_id}:{voice}"
    if not limits.check_idempotency(idempotency_key).allowed:
        await call.answer("⏳ Уже обрабатывается")
        return
    limits.mark_idempotency(idempotency_key)

    tts = TTSService()
    temp = get_temp_manager()

    try:
        # Generate
        result = await tts.synthesize(text, voice)

        # Save temp file
        with await temp.job_context(job_id) as job_dir:
            audio_path = temp.save_audio(job_id, result.audio_bytes, ".mp3")

            # Send to Telegram
            await call.message.answer_audio(
                audio=types.InputFile(str(audio_path)),
                caption=f"🎙 Голос: {voice}\n🔄 Повторить?",
                reply_markup=get_result_keyboard(),
            )
            limits.increment_generation(user_id)

    except Exception as e:
        logger.error("tts_generation_failed", user_id=user_id, error=str(e))
        await call.message.answer("❌ Ошибка генерации. Попробуй позже.")
    finally:
        await tts.close()
        # Cleanup handled by job_context finally