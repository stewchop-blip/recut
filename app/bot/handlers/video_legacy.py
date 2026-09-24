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
from app.services.overlays.templates import TITLES
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

        # 6. Vertical render (PHASE F: user style from DB — same engine
        # as QuickPrep).
        await edit_status("📱 Конвертирую в вертикальный…")
        user_style = dict(background_id="blur", title_text="", brand_corner=False)
        cta_settings = dict(
            cta_position="bottom", cta_mode="end",
            cta_duration_seconds=4.0, cta_start_seconds=0.0,
            cta_size_preset="medium", cta_overlay_type="png",
            cta_enabled=False,
        )
        cta_asset: Path | None = None
        async with db_manager.session() as session:
            s = await UserSettingsRepository(session).get(user_id)
            if s is not None:
                # Style for the vertical render (one engine everywhere).
                from app.services.overlays.templates import BACKGROUNDS
                user_style["background_id"] = (
                    getattr(s, "background_id", None) or "blur")
                # PHASE D: custom title text support
                tid = getattr(s, "title_id", "none") or "none"
                if tid == "custom":
                    user_style["title_text"] = (getattr(s, "custom_title", "") or "").strip()[:100]
                else:
                    t = TITLES.get(tid)
                    user_style["title_text"] = t.text if t else ""
                user_style["brand_corner"] = bool(getattr(s, "brand_corner", False))
                # CTA settings (explicit pass-through, no env dependency)
                cta_settings.update(
                    cta_position=s.cta_position,
                    cta_mode=s.cta_mode,
                    cta_duration_seconds=s.cta_duration_seconds,
                    cta_start_seconds=s.cta_start_seconds,
                    cta_size_preset=getattr(s, "cta_size", None) or "medium",
                    cta_overlay_type=getattr(s, "overlay_type", None) or "png",
                    cta_enabled=bool(s.cta_enabled),
                )
                # Resolve the user's banner (telegram_file_id first —
                # Railway-safe; local path second).
                if cta_settings["cta_enabled"] and s.cta_telegram_file_id:
                    try:
                        tg_file = await bot.get_file(s.cta_telegram_file_id)
                        ot = cta_settings["cta_overlay_type"]
                        ext = {"png": "png", "webp": "webp", "gif": "gif", "mp4": "mp4"}.get(ot := getattr(s, "overlay_type", None) or "png", "png")
                        banner_path = job_dir / f"cta_user.{ext}"
                        await bot.download_file(tg_file.file_path, destination=banner_path)
                        cta_asset = banner_path
                        cta_settings["cta_overlay_type"] = ot
                        logger.info("long_cta_loaded_from_telegram_file_id", user_id=user_id)
                    except Exception as e:
                        logger.warning("long_cta_download_failed", error=str(e)[:200])
                elif cta_settings["cta_enabled"] and s.cta_asset_path:
                    p = Path(s.cta_asset_path)
                    if p.exists():
                        cta_asset = p

        vertical_dir = job_dir / "vertical"
        try:
            vertical_job = await VerticalRenderer(
                background_id=user_style["background_id"],
                title_text=user_style["title_text"],
                brand_corner=user_style["brand_corner"],
            ).render(cut_job, vertical_dir)
        except VerticalRenderError as e:
            await edit_status(f"❌ Не удалось сделать вертикаль: {e}")
            return

        # 7. Final render (subs + CTA + clean) — PHASE F explicit params.
        await edit_status("💬 Субтитры, плашка, чистый экспорт…")

        final_dir = job_dir / "final"
        try:
            final_job = await FinalRenderer().render(
                vertical_clips=vertical_job.clips,
                transcribed=transcribed,
                output_dir=final_dir,
                cta_configured_asset=str(cta_asset) if cta_asset else "",
                cta_position=cta_settings["cta_position"],
                cta_mode=cta_settings["cta_mode"],
                cta_duration_seconds=cta_settings["cta_duration_seconds"],
                cta_start_seconds=cta_settings["cta_start_seconds"],
                cta_size_preset=cta_settings["cta_size_preset"],
                cta_overlay_type=cta_settings["cta_overlay_type"],
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