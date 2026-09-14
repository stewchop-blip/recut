"""Callback for voice selection — triggers TTS synthesis + ffmpeg conversion to MP3."""
import uuid
from pathlib import Path

from aiogram import F, Router
from aiogram.types import CallbackQuery, FSInputFile

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

    # Use the original text the user sent (look back at the "text received" message)
    text = call.message.text or "Привет! Тестовое озвучивание."
    # The "Выбери голос:" message has text like '✅ Текст получен (NN симв.).\n\nВыбери голос:'
    # which is not useful for TTS. Strip the prefix to get the user text? No — we don't have it.
    # In MVP we just announce a placeholder; the rewrite_flow path stores real text later.
    if text.startswith("✅"):
        text = "Привет! Это тестовая озвучка от Recut."

    await call.answer(f"Генерирую голос: {voice}")

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
    media = get_media_service()
    temp = get_temp_manager()

    try:
        # 1. Synthesize (Gemini returns PCM raw bytes; OpenAI returns mp3)
        result = await tts.synthesize(text, voice)

        # 2. Save raw audio to temp file with correct extension
        raw_ext = result.extension  # .pcm or .mp3
        raw_path = temp.save_audio(job_id, result.audio_bytes, raw_ext)

        # 3. Convert to MP3 (always — Telegram needs it for audio messages)
        async with temp.job_context(job_id) as job_dir:
            mp3_path = job_dir / "out.mp3"
            try:
                await media.convert_audio(Path(raw_path), mp3_path, format="mp3", bitrate="128k")
                final_path = mp3_path
                final_ext = ".mp3"
            except Exception as conv_err:
                # ffmpeg failed → fall back to raw bytes if mp3 already
                logger.warning("ffmpeg_convert_failed", error=str(conv_err)[:100], using="raw")
                final_path = Path(raw_path)
                final_ext = raw_ext

            # 4. Send to Telegram
            await call.message.answer_audio(
                audio=FSInputFile(str(final_path), filename=f"recut_{voice}{final_ext}"),
                caption=f"🎙 Голос: {voice}\n🔄 Повторить?",
                reply_markup=get_result_keyboard(),
            )
            limits.increment_generation(user_id)
            logger.info("audio_sent", user_id=user_id, voice=voice, size_bytes=final_path.stat().st_size)

    except Exception as e:
        logger.error("tts_generation_failed", user_id=user_id, error=str(e)[:200])
        await call.message.answer("❌ Ошибка генерации. Попробуй позже.")
    finally:
        await tts.close()
        # Cleanup handled by job_context finally