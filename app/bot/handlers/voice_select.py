"""Callback for voice selection — pulls user text from FSM and synthesizes."""
import uuid
from pathlib import Path

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile

from app.bot.keyboards.inline import get_result_keyboard
from app.bot.states import RecutStates
from app.core.limits import get_limits_manager
from app.core.logging import get_logger
from app.services.media.ffmpeg import get_media_service
from app.services.tts import TTSService
from app.utils.temp import get_temp_manager

router = Router()
logger = get_logger(__name__)


@router.callback_query(F.data.startswith("voice:"))
async def on_voice_select(call: CallbackQuery, state: FSMContext) -> None:
    voice = call.data.split(":", 1)[1]
    user_id = call.from_user.id if call.from_user else 0
    job_id = str(uuid.uuid4())

    # Pull the actual user text from FSM (set by text_input handler)
    data = await state.get_data()
    text = (data.get("text") or "").strip()
    if not text:
        await call.answer("⚠️ Сначала отправь текст.", show_alert=True)
        return

    # If user somehow re-uses an old voice button, drop them back to a clean state
    current_state = await state.get_state()
    if current_state != RecutStates.awaiting_voice.state:
        await call.answer("⚠️ Сначала отправь текст.", show_alert=True)
        return

    await call.answer(f"Генерирую голос: {voice}")

    limits = get_limits_manager()
    quota = limits.check_daily_generations(user_id)
    if not quota.allowed:
        await call.message.answer("❌ Лимит исчерпан.")
        await state.clear()
        return

    # Idempotency: one generation per (user, prompt_message, voice)
    prompt_msg_id = data.get("prompt_message_id") or call.message.message_id
    idempotency_key = f"voice:{user_id}:{prompt_msg_id}:{voice}"
    if not limits.check_idempotency(idempotency_key).allowed:
        await call.answer("⏳ Уже обрабатывается")
        return
    limits.mark_idempotency(idempotency_key)

    tts = TTSService()
    media = get_media_service()
    temp = get_temp_manager()

    try:
        # 1. Synthesize (gpt-audio-mini returns raw PCM16)
        result = await tts.synthesize(text, voice)

        # 2. Save raw audio with correct extension
        raw_ext = result.extension  # .pcm
        raw_path = temp.save_audio(job_id, result.audio_bytes, raw_ext)

        # 3. Wrap into WAV (Telegram-friendly, no codec needed in ffmpeg)
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
            await call.message.answer_audio(
                audio=FSInputFile(str(final_path), filename=f"recut_{voice}{final_ext}"),
                caption=f"🎙 Голос: {voice}\n📝 {text[:120]}{'…' if len(text) > 120 else ''}",
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

    except Exception as e:
        logger.error("tts_generation_failed", user_id=user_id, error=str(e)[:200])
        await call.message.answer("❌ Ошибка генерации. Попробуй позже.")
    finally:
        await tts.close()
        # Clear FSM so the next /text starts a fresh flow
        await state.clear()