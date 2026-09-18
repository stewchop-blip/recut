"""Legacy long-video pipeline — Whisper + LLM + cut + subtitles + CTA.

Used when the user picks "✂️ Найти лучшие моменты". This is the original
full pipeline (Stages A-K from the migration); kept intact so Quick Prep
can use the FFmpeg-only path without forcing Whisper.

The function `run_long_pipeline()` is called from `video.py` when the
user clicks the "Analyze long video" action.
"""
import asyncio
import json
import uuid
from pathlib import Path

from aiogram import Bot
from aiogram.types import Message

from app.core.config import get_settings
from app.core.logging import get_logger
from app.database.repositories import JobRepository, UserSettingsRepository
from app.database.session import db_manager
from app.pipeline.analyser import Analyser, AnalyserError
from app.pipeline.clip_cutter import ClipCutter, ClipCutterError
from app.pipeline.extractor import AudioExtractionError, AudioExtractor
from app.pipeline.final_renderer import FinalClip, FinalJob, FinalRenderer
from app.pipeline.transcriber import Transcriber, TranscriberError
from app.pipeline.vertical_renderer import VerticalRenderer, VerticalRenderError
from app.services.media import get_probe_service
from app.services.sender import TelegramSender
from app.utils.temp import get_temp_manager

logger = get_logger(__name__)


async def run_long_pipeline(
    message: Message,
    bot: Bot,
    pending,  # _PendingJob from video.py
    user_id: int,
) -> None:
    """Run the original long-video pipeline on a stored input file."""
    settings = get_settings()
    input_path = Path(pending.input_path)
    job_dir = Path(pending.job_dir)

    async def edit_status(text: str) -> None:
        try:
            await message.edit_text(text)
        except Exception:
            pass

    try:
        # 1. Probe
        await edit_status("🎬 Проверяю видео…")
        probe = await get_probe_service().probe(input_path)

        # 2. Audio extraction
        await edit_status("🎧 Извлекаю звук…")
        wav_path = job_dir / "audio.wav"
        try:
            extracted = await AudioExtractor().extract(input_path, wav_path)
        except AudioExtractionError as e:
            await edit_status(f"❌ Не удалось извлечь звук: {e}")
            return

        # 3. Transcribe
        await edit_status("🎤 Распознаю речь…")
        try:
            transcribed = await Transcriber().transcribe(wav_path, language="ru")
        except TranscriberError as e:
            await edit_status(f"❌ Не удалось распознать речь: {e}")
            return

        if not transcribed.segments:
            await edit_status("❌ В видео не нашлось речи.")
            return

        # 4. Analyze
        await edit_status("🧠 Ищу интересные моменты…")
        try:
            analysed = await Analyser().analyse(transcribed)
        except AnalyserError as e:
            await edit_status(f"❌ Не удалось подобрать моменты: {e}")
            return

        # 5. Cut
        await edit_status("✂️ Нарезаю клипы…")
        try:
            cut_job = await ClipCutter().cut(
                analysed=analysed,
                transcribed=transcribed,
                source_video=input_path,
                output_dir=job_dir,
            )
        except ClipCutterError as e:
            await edit_status(f"❌ Не удалось нарезать: {e}")
            return

        # 6. Vertical render
        await edit_status("📱 Конвертирую в вертикальный…")
        vertical_dir = job_dir / "vertical"
        try:
            vertical_job = await VerticalRenderer().render(cut_job, vertical_dir)
        except VerticalRenderError as e:
            await edit_status(f"❌ Не удалось сделать вертикаль: {e}")
            return

        # 7. Final render (subs + CTA + clean)
        await edit_status("💬 Субтитры, CTA, чистый экспорт…")
        cta_asset: Path | None = None
        cta_enabled = False
        cta_position = "bottom"
        cta_mode = "end"
        cta_duration_seconds = 4.0
        cta_start_seconds = 0.0
        async with db_manager.session() as session:
            s = await UserSettingsRepository(session).get(user_id)
            if s is not None:
                cta_enabled = s.cta_enabled
                cta_position = s.cta_position
                cta_mode = s.cta_mode
                cta_duration_seconds = s.cta_duration_seconds
                cta_start_seconds = s.cta_start_seconds
                if s.cta_asset_path:
                    p = Path(s.cta_asset_path)
                    if p.exists():
                        cta_asset = p

        final_dir = job_dir / "final"
        try:
            # We pass CTA settings through env for the legacy path
            from app.core.config import Settings as _S
            # Override env-derived settings via an in-place settings snapshot
            # by directly passing them to FinalRenderer's runtime. For
            # simplicity, the legacy path uses the env-driven CTA when
            # DB settings are absent (and ignores them when present
            # via CTA_ASSET_PATH in the FinalRenderer's CTAService
            # which already reads CTA_ASSET_PATH). Here we pass
            # cta_asset via the FinalRenderer's render().
            final_job = await FinalRenderer().render(
                vertical_clips=vertical_job.clips,
                transcribed=transcribed,
                output_dir=final_dir,
                cta_configured_asset=str(cta_asset) if cta_asset else "",
            )
        except Exception as e:
            await edit_status(f"❌ Не удалось финализировать: {e}")
            return

        # 8. Send
        await edit_status("📤 Отправляю клипы…")
        sender = TelegramSender(bot)
        send_result = await sender.send(
            final_job=final_job,
            chat_id=pending.chat_id,
            reply_to_message_id=pending.status_message_id,
        )

        if send_result.sent:
            await edit_status(
                f"✅ Нашёл {len(send_result.sent)} моментов.\n\n"
                f"Готово. Исходник и временные файлы удалены."
            )
            async with db_manager.session() as session:
                await JobRepository(session).mark_completed(
                    pending.job_id, clips_generated=len(send_result.sent),
                )
        else:
            await edit_status("❌ Не удалось отправить клипы.")

    finally:
        try:
            get_temp_manager().cleanup_job(job_dir.name)
        except Exception:
            pass