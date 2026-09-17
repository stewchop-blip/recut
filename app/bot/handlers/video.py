"""Video intake handler.

Accepts `message.video`, `message.video_note`, and `message.document`
with a video MIME. Flow:

1. Validate Telegram-reported file size + MIME.
2. Make sure user doesn't already have a Job in flight (DB check).
3. Open a `JobRepository` session, create a `Job` row.
4. Send a status message ("⏳ Загружаю…").
5. Download the file from Telegram to `temp_dir/{job_id}/input.mp4`.
6. Run ffprobe to confirm duration / dimensions.
7. Reject early if duration > limit.
8. Update Job with source_* metadata; mark JobStatus.PROBING → done.

Realtime pipeline stages (transcribe/analyze/cut/render) are NOT
triggered here — that's wired up in stage C+.

Errors are reported as friendly messages; full detail goes to logs.
"""
import uuid
from datetime import datetime, timezone

from aiogram import Bot, F, Router, types
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import get_logger
from app.database.models import Job, JobStatus
from app.database.repositories import JobRepository
from app.database.session import db_manager
from app.pipeline.downloader import VideoDownloader
from app.pipeline.extractor import AudioExtractionError, AudioExtractor
from app.pipeline.transcriber import Transcriber, TranscriberError
from app.pipeline.analyser import Analyser, AnalyserError
from app.pipeline.clip_cutter import ClipCutter, ClipCutterError
from app.pipeline.validator import VideoValidationError, VideoValidator
from app.services.media import get_probe_service
from app.utils.temp import get_temp_manager

router = Router()
logger = get_logger(__name__)


# Accept video, video_note (round video), and video-as-document.
@router.message(F.video | F.video_note | F.document)
async def on_video_message(message: types.Message, bot: Bot) -> None:
    user_id: int = message.conf.get("telegram_user_id") or (message.from_user.id if message.from_user else 0)
    if not user_id:
        return

    settings = get_settings()
    validator = VideoValidator()

    # ---- 1. Validate the Telegram attachment metadata ----
    attachment = _pick_attachment(message)
    if attachment is None:
        # Not actually a video — ignore silently (start handler covers text).
        return

    declared_size = int(getattr(attachment, "file_size", 0) or 0)
    mime = getattr(attachment, "mime_type", None)
    filename = getattr(attachment, "file_name", None)

    try:
        validator.check_size(declared_size)
        validator.check_mime(mime)
    except VideoValidationError as e:
        await message.answer(f"❌ {e}")
        logger.warning("video_rejected_pre_download", user_id=user_id, code=e.code)
        return

    # ---- 2. Concurrent-job guard (DB-backed) ----
    async with db_manager.session() as session:
        repo = JobRepository(session)
        if await repo.has_active_job(user_id):
            await message.answer(
                "⏳ У тебя уже есть видео в обработке.\n"
                "Подожди, пока оно закончится, или отправь /cancel."
            )
            logger.info("video_rejected_concurrent", user_id=user_id)
            return

        # ---- 3. Create Job row ----
        job = await repo.create(
            telegram_user_id=user_id,
            telegram_chat_id=message.chat.id,
            source_message_id=message.message_id,
            source_filename=filename,
            source_bytes=declared_size or None,
        )
        job_id = job.id

    # ---- 4. Status message — we'll keep editing this through the pipeline ----
    status_msg = await message.answer(
        f"⏳ Загружаю видео…\n"
        f"🆔 Job #{job_id}"
    )

    # ---- 5. Download ----
    temp = get_temp_manager()
    downloader = VideoDownloader(bot)
    job_dir = temp._job_dir(f"job_{job_id}_{uuid.uuid4().hex[:8]}")
    job_dir.mkdir(parents=True, exist_ok=True)

    try:
        input_path = await downloader.download_to_job_dir(
            source=message, job_dir=job_dir, filename="input.mp4",
        )
    except Exception as e:
        logger.error("video_download_failed", user_id=user_id, job_id=job_id, error=str(e)[:200])
        await _mark_failed(job_id, code="DOWNLOAD_FAILED", detail=str(e)[:200])
        await status_msg.edit_text(
            f"❌ Не удалось скачать видео.\n"
            f"Попробуй ещё раз или отправь другое видео.\n\n"
            f"🆔 Job #{job_id}"
        )
        return

    # ---- 6. Probe ----
    actual_size = input_path.stat().st_size
    try:
        validator.check_size(actual_size)  # safety check after download
        probe = await get_probe_service().probe(input_path)
    except VideoValidationError as e:
        await _mark_failed(job_id, code=e.code, detail=str(e))
        await status_msg.edit_text(f"❌ {e}\n\n🆔 Job #{job_id}")
        temp.cleanup_job(job_dir.name)
        return
    except Exception as e:
        logger.error("video_probe_failed", user_id=user_id, job_id=job_id, error=str(e)[:200])
        await _mark_failed(job_id, code="PROBE_FAILED", detail=str(e)[:200])
        await status_msg.edit_text(
            f"❌ Не удалось разобрать видео. Возможно, файл повреждён.\n\n"
            f"🆔 Job #{job_id}"
        )
        temp.cleanup_job(job_dir.name)
        return

    try:
        validator.check_duration(probe.duration_seconds)
    except VideoValidationError as e:
        await _mark_failed(job_id, code=e.code, detail=str(e))
        await status_msg.edit_text(f"❌ {e}\n\n🆔 Job #{job_id}")
        temp.cleanup_job(job_dir.name)
        return

    # ---- 7. Persist source metadata on Job ----
    async with db_manager.session() as session:
        repo = JobRepository(session)
        await repo.update_source_meta(
            job_id=job_id,
            source_duration_seconds=probe.duration_seconds,
            source_width=probe.width,
            source_height=probe.height,
        )

    # ---- 7b. Extract audio track (Stage C) ----
    wav_path = job_dir / "audio.wav"
    if not probe.has_audio:
        # Mark job failed: speech-to-text needs audio.
        await _mark_failed(job_id, code="NO_AUDIO", detail="Source has no audio track")
        await status_msg.edit_text(
            f"❌ Видео без звука — не могу распознать речь.\n\n"
            f"🆔 Job #{job_id}"
        )
        temp.cleanup_job(job_dir.name)
        return

    try:
        extracted = await AudioExtractor().extract(
            video_path=input_path,
            output_wav=wav_path,
            has_audio=True,
        )
    except AudioExtractionError as e:
        logger.error("audio_extract_failed", user_id=user_id, job_id=job_id, error=str(e)[:200])
        await _mark_failed(job_id, code="AUDIO_EXTRACT_FAILED", detail=str(e)[:200])
        await status_msg.edit_text(
            f"❌ Не удалось извлечь звук.\n\n🆔 Job #{job_id}"
        )
        temp.cleanup_job(job_dir.name)
        return

    logger.info(
        "audio_extracted",
        job_id=job_id,
        path=str(extracted.path),
        duration_s=round(extracted.duration_seconds, 1),
        bytes=extracted.path.stat().st_size,
    )

    # ---- 7c. Transcribe audio (Stage D) ----
    await status_msg.edit_text(
        f"🎧 Распознаю речь…\n\n🆔 Job #{job_id}"
    )

    try:
        transcribed = await Transcriber().transcribe(
            audio_path=extracted.path,
            language="ru",
            beam_size=1,
            word_timestamps=False,
        )
    except TranscriberError as e:
        logger.error("transcribe_failed", user_id=user_id, job_id=job_id, error=str(e)[:200])
        await _mark_failed(job_id, code="TRANSCRIBE_FAILED", detail=str(e)[:200])
        await status_msg.edit_text(
            f"❌ Не удалось распознать речь.\n\n🆔 Job #{job_id}"
        )
        temp.cleanup_job(job_dir.name)
        return

    if not transcribed.segments:
        await _mark_failed(job_id, code="NO_SPEECH", detail="Whisper found no speech segments")
        await status_msg.edit_text(
            f"❌ В видео не нашлось речи. Попробуй другое видео.\n\n🆔 Job #{job_id}"
        )
        temp.cleanup_job(job_dir.name)
        return

    # Persist transcript JSON next to audio for Stage E
    import json
    transcript_path = job_dir / "transcript.json"
    transcript_path.write_text(
        json.dumps(
            {
                "language": transcribed.language,
                "duration_seconds": transcribed.duration_seconds,
                "model": transcribed.model,
                "segments": [
                    {"start": s.start, "end": s.end, "text": s.text}
                    for s in transcribed.segments
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    logger.info(
        "transcribed",
        job_id=job_id,
        segments=len(transcribed.segments),
        language=transcribed.language,
        duration_s=round(transcribed.duration_seconds, 1),
        chars=len(transcribed.raw_text),
    )

    # ---- 7d. Pick interesting moments via LLM (Stage E) ----
    await status_msg.edit_text(
        f"🧠 Ищу интересные моменты…\n\n🆔 Job #{job_id}"
    )

    try:
        analysed = await Analyser().analyse(transcribed)
    except AnalyserError as e:
        logger.error("analyse_failed", user_id=user_id, job_id=job_id, error=str(e)[:200])
        await _mark_failed(job_id, code="ANALYSE_FAILED", detail=str(e)[:200])
        await status_msg.edit_text(
            f"❌ Не удалось подобрать моменты.\n\n🆔 Job #{job_id}"
        )
        temp.cleanup_job(job_dir.name)
        return

    # Persist chosen clips next to transcript.
    clips_path = job_dir / "clips.json"
    clips_path.write_text(
        json.dumps(
            {
                "model": analysed.model,
                "language": analysed.language,
                "clips": [
                    {"start": c.start, "end": c.end, "title": c.title,
                     "hook": c.hook, "reason": c.reason}
                    for c in analysed.clips
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    logger.info(
        "clips_selected",
        job_id=job_id,
        count=len(analysed.clips),
        model=analysed.model,
    )

    # ---- 7e. Cut source video into N MP4 clips (Stage F) ----
    await status_msg.edit_text(
        f"✂️ Нарезаю клипы…\n\n🆔 Job #{job_id}"
    )

    try:
        cut_job = await ClipCutter().cut(
            analysed=analysed,
            transcribed=transcribed,
            source_video=input_path,
            output_dir=job_dir,
        )
    except ClipCutterError as e:
        logger.error("clip_cut_failed", user_id=user_id, job_id=job_id, error=str(e)[:200])
        await _mark_failed(job_id, code="CUT_FAILED", detail=str(e)[:200])
        await status_msg.edit_text(
            f"❌ Не удалось нарезать клипы.\n\n🆔 Job #{job_id}"
        )
        temp.cleanup_job(job_dir.name)
        return

    logger.info(
        "clips_cut",
        job_id=job_id,
        count=len(cut_job.clips),
        dir=str(job_dir),
    )

    # ---- 8. Acknowledge and signal next stage ----
    duration_str = _format_duration(probe.duration_seconds)
    clips_preview = "\n".join(
        f"  {c.index}. [{_format_duration(c.source_start)}–{_format_duration(c.source_end)}] "
        f"{c.candidate.title} ({_format_duration(c.source_end - c.source_start)})"
        for c in cut_job.clips
    )
    summary = (
        f"✅ Видео принято.\n\n"
        f"⏱ Длительность: {duration_str}\n"
        f"📐 Размер: {probe.width}×{probe.height}\n"
        f"🎤 Речь: {len(transcribed.segments)} сегментов\n"
        f"✂️ Нарезано клипов: {len(cut_job.clips)}\n\n"
        f"{clips_preview}\n\n"
        f"⏳ Этап F завершён — следующий шаг: вертикальный формат 9:16.\n\n"
        f"🆔 Job #{job_id}"
    )
    await status_msg.edit_text(summary)

    logger.info(
        "video_accepted",
        job_id=job_id,
        user_id=user_id,
        duration_s=round(probe.duration_seconds, 1),
        width=probe.width,
        height=probe.height,
        bytes=actual_size,
        job_dir=str(job_dir),
    )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _pick_attachment(message: types.Message):
    """Return the biggest video attachment, or None."""
    candidates = []
    if message.video:
        candidates.append(message.video)
    if message.video_note:
        candidates.append(message.video_note)
    if message.document and message.document.mime_type and message.document.mime_type.startswith("video/"):
        candidates.append(message.document)
    if not candidates:
        return None
    return max(candidates, key=lambda a: getattr(a, "file_size", 0) or 0)


async def _mark_failed(job_id: int, code: str, detail: str) -> None:
    """Mark a job as failed in the DB, swallowing any DB errors."""
    try:
        async with db_manager.session() as session:
            repo = JobRepository(session)
            await repo.mark_failed(job_id=job_id, error_code=code, error_detail=detail)
    except Exception as e:
        logger.error("mark_failed_db_error", job_id=job_id, error=str(e)[:120])


def _format_duration(seconds: float) -> str:
    """Format seconds as e.g. '12:34' or '1:02:03'."""
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"
