"""Callback for voice selection — pulls user text from store and synthesizes."""
import uuid
from pathlib import Path

from aiogram import F, Router
from aiogram.types import CallbackQuery, FSInputFile

from app.bot.keyboards.inline import get_result_keyboard
from app.bot.user_text_store import get_user_text_store
from app.core.limits import get_limits_manager
from app.core.logging import get_logger
from app.services.media.ffmpeg import get_media_service
from app.services.tts import TTSService
from app.utils.temp import get_temp_manager

router = Router()
logger = get_logger(__name__)


@router.callback_query(F.data.startswith("voice:"))
async def on_voice_select(call: CallbackQuery) -> None:
    voice = call.data.split(":", 1)[1]
    user_id = call.from_user.id if call.from_user else 0
    job_id = str(uuid.uuid4())

    # Pull the actual user text from the store
    pending = await get_user_text_store().get(user_id)
    if pending is None or not pending.text.strip():
        await call.answer("⚠️ Сначала отправь текст.", show_alert=True)
        return

    text = pending.text

    await call.answer(f"Генерирую: {voice}")

    limits = get_limits_manager()
    quota = limits.check_daily_generations(user_id)
    if not quota.allowed:
        await call.message.answer("❌ Лимит исчерпан на сегодня.")
        return

    # Idempotency: one generation per (user, prompt_message, voice)
    idempotency_key = f"voice:{user_id}:{pending.prompt_message_id}:{voice}"
    if not limits.check_idempotency(idempotency_key).allowed:
        await call.answer("⏳ Уже генерирую")
        return
    limits.mark_idempotency(idempotency_key)

    tts = TTSService()
    media = get_media_service()
    temp = get_temp_manager()

    try:
        # 1. Synthesize (gpt-audio-mini returns raw PCM16 @ 24 kHz mono)
        result = await tts.synthesize(text, voice)

        # 2. Save raw audio
        raw_ext = result.extension  # .pcm
        raw_path = temp.save_audio(job_id, result.audio_bytes, raw_ext)

        # 3. Wrap into WAV for Telegram (no codec needed)
        async with temp.job_context(job_id) as job_dir:
            wav_path = job_dir / "out.wav"
            try:
                if raw_ext == ".pcm":
                    await media.convert_audio(
                        Path(raw_path),
                        wav_path,
                        format="wav",
                        input_format="s16le",
                        sample_rate=24_000,
                        channels=1,
                    )
                else:
                    await media.convert_audio(
                        Path(raw_path),
                        wav_path,
                        format="wav",
                    )
                final_path = wav_path
                final_ext = ".wav"
            except Exception as conv_err:
                logger.warning(
                    "ffmpeg_convert_failed",
                    error=str(conv_err)[:200],
                    using="raw",
                )
                final_path = Path(raw_path)
                final_ext = raw_ext

            # 4. Send to Telegram
            preview = text[:100] + ("…" if len(text) > 100 else "")
            await call.message.answer_audio(
                audio=FSInputFile(str(final_path), filename=f"recut_{voice}{final_ext}"),
                caption=(
                    f"🎙 Голос: <b>{voice}</b>\n"
                    f"📝 «{preview}»\n\n"
                    f"Отправь новый текст или нажми /start"
                ),
                reply_markup=get_result_keyboard(),
            )
            limits.increment_generation(user_id)
            logger.info(
                "audio_sent",
                user_id=user_id,
                voice=voice,
                size_bytes=final_path.stat().st_size,
                text_length=len(text),
            )

        # 5. Clear pending text so the next /text starts a fresh flow
        await get_user_text_store().clear(user_id)

    except Exception as e:
        logger.error("tts_generation_failed", user_id=user_id, error=str(e)[:200])
        await call.message.answer(
            "❌ Не удалось озвучить. Попробуй ещё раз или нажми /start."
        )
    finally:
        await tts.close()