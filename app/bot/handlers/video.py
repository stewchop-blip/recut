"""Video intake + Quick Prep + Settings handlers.

New UX flow:

  user sends video
    -> accept + validate + create Job row
    -> save downloaded file to job_dir/input.mp4
    -> send action menu:
        [ 🚀 Подготовить видео ]
        [ ✂️ Найти лучшие моменты ]  [ ⚙️ Настройки ]

  user clicks "🚀 Подготовить видео"
    -> Quick Prep pipeline (FFmpeg only: probe -> vertical -> CTA -> clean)
    -> send final MP4 in chat

  user clicks "✂️ Найти лучшие моменты"
    -> Full long-video pipeline (Whisper + LLM + cut + subtitles)
    -> send N short clips

  user clicks "⚙️ Настройки"
    -> show Settings menu, persist via UserSettingsRepository

The Telegram 20 MB Bot API limit applies — this code can't work around
that without a self-hosted Bot API server. The handler validates file size
before starting any work.
"""
import shutil
import uuid
from pathlib import Path

from aiogram import Bot, F, Router, types
from aiogram.types import CallbackQuery

from app.bot.keyboards.inline import (
    ACTION_MENU,
    POSITION_MENU,
    SETTINGS_MENU,
    SHORT_ACTION_MENU,
    TIMING_MENU,
    preview_keyboard,
)
from app.core.config import get_settings
from app.core.logging import get_logger
from app.database.repositories import (
    JobRepository,
    UserSettingsRepository,
)
from app.database.session import db_manager
from app.pipeline.downloader import VideoDownloader
from app.pipeline.quick_prep import QuickPrepPipeline
from app.pipeline.url_downloader import DownloaderService, URLDownloadError, URLDownloadResult
from app.services.media.probe import get_probe_service
from app.services.sender import TelegramSender
from app.services.transcription.faster_whisper import get_transcription_service
from app.utils.temp import get_temp_manager

router = Router()
logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CTA_POSITIONS = {
    "top": "Сверху",
    "bottom": "Снизу",
    "top_left": "Сверху слева",
    "top_right": "Сверху справа",
    "bottom_left": "Снизу слева",
    "bottom_right": "Снизу справа",
}

CTA_TIMING = {
    "full": "Весь ролик",
    "start_3": "Первые 3 сек",
    "end_3": "Последние 3 сек",
    "end_5": "Последние 5 сек",
}

# Where we keep user-uploaded CTA banners
_USER_ASSETS_DIR = Path("/tmp/recut/users")


def _user_asset_path(user_id: int) -> Path:
    """Directory where per-user CTA assets live."""
    p = _USER_ASSETS_DIR / str(user_id)
    p.mkdir(parents=True, exist_ok=True)
    return p


async def _fail_job(job_id: int, code: str, detail: str = "") -> None:
    """Mark a job as FAILED so it doesn't block the user via has_active_job.

    Must be called in EVERY error path after a job has been created.
    """
    try:
        async with db_manager.session() as session:
            repo = JobRepository(session)
            await repo.mark_failed(job_id, code, detail)
    except Exception as e:
        logger.warning("fail_job_db_error", job_id=job_id, error=str(e)[:120])


def _fmt_duration(seconds: float) -> str:
    """Format seconds as M:SS or H:MM:SS."""
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


# ---------------------------------------------------------------------------
# Step 1 — accept video, save to job_dir, show action menu
# ---------------------------------------------------------------------------

@router.message(F.video | F.video_note | (F.document & F.document.mime_type.startswith("video/")))
async def on_video_message(message: types.Message, bot: Bot) -> None:
    user_id: int = message.from_user.id if message.from_user else 0
    if not user_id:
        return

    settings = get_settings()

    # Detect attachment
    attachment = _pick_attachment(message)
    if attachment is None:
        return

    declared_size = int(getattr(attachment, "file_size", 0) or 0)
    mime = getattr(attachment, "mime_type", None)
    filename = getattr(attachment, "file_name", None) or "video.mp4"

    # Telegram Bot API limit: 20 MB for files via getFile.
    TELEGRAM_BOT_API_LIMIT = 20 * 1024 * 1024
    if declared_size > TELEGRAM_BOT_API_LIMIT:
        await message.answer(
            "❌ Видео больше 20 МБ.\n\n"
            "Telegram Bot API сейчас не позволяет боту скачивать файлы больше этого лимита. "
            "Это ограничение платформы, не наше.\n\n"
            "Попробуй сжать видео до 20 МБ или отправь ссылку на файл."
        )
        return

    # Telegram MIME hint (some attachments omit it; treat as ok)
    if mime and mime != "application/octet-stream" and not mime.startswith("video/"):
        await message.answer(
            f"❌ Неподдерживаемый формат: {mime}.\n\n"
            f"Отправь MP4 / MOV / MKV / WebM."
        )
        return

    # Concurrency guard
    async with db_manager.session() as session:
        repo = JobRepository(session)
        if await repo.has_active_job(user_id):
            await message.answer(
                "⏳ У тебя уже есть видео в обработке.\n"
                "Подожди, пока оно закончится."
            )
            return

        job = await repo.create(
            telegram_user_id=user_id,
            telegram_chat_id=message.chat.id,
            source_message_id=message.message_id,
            source_filename=filename,
            source_bytes=declared_size or None,
        )
        job_id = job.id

    status_msg = await message.answer(
        f"⏳ Загружаю видео…\n\n🆔 Job #{job_id}"
    )

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
        await _fail_job(job_id, "DOWNLOAD_FAILED", str(e)[:500])
        await status_msg.edit_text(
            f"❌ Не удалось скачать видео.\n\nПопробуй ещё раз.\n\n🆔 Job #{job_id}"
        )
        temp.cleanup_job(job_dir.name)
        return

    actual_size = input_path.stat().st_size

    # Probe for UX metadata (duration, resolution).
    try:
        probe = get_probe_service()
        meta = await probe.probe(input_path)
        duration_sec = meta.duration_seconds
        w, h = meta.width, meta.height
    except Exception:
        duration_sec = 0.0
        w, h = 0, 0

    # Mark job DOWNLOADING done; remember source path on the job
    async with db_manager.session() as session:
        repo = JobRepository(session)
        await repo.set_status(
            job_id=job_id,
            status=__import__("app.database.models", fromlist=["JobStatus"]).JobStatus.PENDING,
            status_message_id=status_msg.message_id,
        )
        # Persist a small metadata note (actual size)
        from sqlalchemy import update as _u
        from app.database.models import Job as _Job
        await session.execute(
            _u(_Job).where(_Job.id == job_id).values(source_bytes=actual_size)
        )

    # Show action menu with video info.
    dur_str = _fmt_duration(duration_sec) if duration_sec > 0 else "—"
    res_str = f"{w}\u00d7{h}" if w > 0 and h > 0 else "—"
    text = f"\U0001f3ac \u0412\u0438\u0434\u0435\u043e \u043f\u043e\u043b\u0443\u0447\u0435\u043d\u043e\n"
    if duration_sec > 0:
        text += f"\u23f1 {dur_str}\n"
    if w > 0 and h > 0:
        text += f"\U0001f4d0 {res_str}\n"
    text += "\n\u0412\u044b\u0431\u0435\u0440\u0438 \u0434\u0435\u0439\u0441\u0442\u0432\u0438\u0435:"

    # Decide menu: short videos get Quick Prep only.
    SMART_CLIPS_MIN_SECONDS = 120
    menu = ACTION_MENU if duration_sec >= SMART_CLIPS_MIN_SECONDS else SHORT_ACTION_MENU
    await status_msg.edit_text(text, reply_markup=menu)

    # Stash the job info on a tiny in-memory store so callbacks can find it
    _pending_jobs[user_id] = _PendingJob(
        job_id=job_id,
        chat_id=message.chat.id,
        input_path=str(input_path),
        job_dir=str(job_dir),
        status_message_id=status_msg.message_id,
    )


@router.message(F.text)
async def on_url_message(message: types.Message, bot: Bot) -> None:
    """Accept a supported video URL, download, then show actions."""
    user_id: int = message.from_user.id if message.from_user else 0
    if not user_id:
        return
    text = (message.text or "").strip()
    if not text.lower().startswith("https://"):
        return
    svc = DownloaderService()
    try:
        svc._validate(text)
    except Exception:
        return

    async with db_manager.session() as session:
        repo = JobRepository(session)
        if await repo.has_active_job(user_id):
            await message.answer(
                "⏳ У тебя уже есть видео в обработке.\nПодожди, пока оно закончится."
            )
            return
        job = await repo.create(
            telegram_user_id=user_id,
            telegram_chat_id=message.chat.id,
            source_message_id=message.message_id,
            source_filename="url.mp4",
            source_bytes=None,
        )
        job_id = job.id

    status_msg = await message.answer(f"⏳ Скачиваю видео…\n\n🆔 Job #{job_id}")
    temp = get_temp_manager()
    job_dir = temp._job_dir(f"job_{job_id}_{uuid.uuid4().hex[:8]}")
    job_dir.mkdir(parents=True, exist_ok=True)

    try:
        result = await svc.download(text, job_dir)
    except URLDownloadError as e:
        logger.error("url_download_failed", user_id=user_id, job_id=job_id, error=str(e)[:200])
        await _fail_job(job_id, "URL_DOWNLOAD_FAILED", str(e)[:500])
        await status_msg.edit_text(
            f"❌ Не удалось скачать видео по ссылке.\n\n"
            f"Попробуй другую ссылку или исходник.\n\n"
            f"🆔 Job #{job_id}"
        )
        temp.cleanup_job(job_dir.name)
        return
    except Exception as e:
        logger.error("url_download_unexpected_error", user_id=user_id, job_id=job_id, error=str(e)[:200])
        await _fail_job(job_id, "URL_DOWNLOAD_FAILED", str(e)[:500])
        await status_msg.edit_text(
            f"❌ Не удалось скачать видео.\n\n"
            f"Попробуй ещё раз.\n\n"
            f"🆔 Job #{job_id}"
        )
        temp.cleanup_job(job_dir.name)
        return

    async with db_manager.session() as session:
        repo = JobRepository(session)
        await repo.set_status(
            job_id=job_id,
            status=__import__("app.database.models", fromlist=["JobStatus"]).JobStatus.PENDING,
            status_message_id=status_msg.message_id,
        )

    _pending_jobs[user_id] = _PendingJob(
        job_id=job_id,
        chat_id=message.chat.id,
        input_path=str(result.path),
        job_dir=str(job_dir),
        status_message_id=status_msg.message_id,
    )
    await status_msg.edit_text(
        f"✅ Видео готово ({result.size_bytes // 1024 // 1024} МБ).\n\nВыбери действие:",
        reply_markup=URL_ACTION_MENU,
    )


# ---------------------------------------------------------------------------
# In-memory pending-job store (process-local, no DB column needed)
# ---------------------------------------------------------------------------

class _PendingJob:
    __slots__ = ("job_id", "chat_id", "input_path", "job_dir", "status_message_id")

    def __init__(self, job_id: int, chat_id: int, input_path: str, job_dir: str,
                 status_message_id: int) -> None:
        self.job_id = job_id
        self.chat_id = chat_id
        self.input_path = input_path
        self.job_dir = job_dir
        self.status_message_id = status_message_id


_pending_jobs: dict[int, _PendingJob] = {}


def _get_pending(user_id: int) -> _PendingJob | None:
    """Return pending job WITHOUT removing it (peek).

    Removal happens only on SUCCESS / CANCEL / explicit terminal cleanup.
    """
    return _pending_jobs.get(user_id)


def _pop_pending(user_id: int) -> _PendingJob | None:
    return _pending_jobs.pop(user_id, None)


# ---------------------------------------------------------------------------
# Step 2 — callbacks
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "action:quick_prep")
async def on_quick_prep(call: CallbackQuery) -> None:
    user_id = call.from_user.id if call.from_user else 0
    pending = _get_pending(user_id)
    if pending is None:
        await call.answer("⚠️ Сначала отправь видео.", show_alert=True)
        return
    await call.answer("🚀 Запускаю…")

    settings = get_settings()
    input_path = Path(pending.input_path)
    job_dir = Path(pending.job_dir)

    await _edit_status(call.message, "🎬 Подготавливаю…\n\n⏳ FFmpeg в работе…")

    # Load user's CTA settings
    cta_asset: Path | None = None
    cta_enabled = False
    cta_position = "bottom"
    cta_mode = "end"
    cta_duration_seconds = 4.0
    cta_start_seconds = 0.0

    async with db_manager.session() as session:
        srepo = UserSettingsRepository(session)
        s = await srepo.get(user_id)
        if s is not None:
            cta_enabled = s.cta_enabled
            cta_position = s.cta_position
            cta_mode = s.cta_mode
            cta_duration_seconds = s.cta_duration_seconds
            cta_start_seconds = s.cta_start_seconds

        # Resolve CTA asset: prefer telegram_file_id (Railway-safe),
        # then local path, then default static banner.
        if cta_enabled:
            bot_instance = call.bot
            if s is not None and s.cta_telegram_file_id:
                try:
                    tg_file = await bot_instance.get_file(s.cta_telegram_file_id)
                    banner_path = job_dir / "cta_user.png"
                    await bot_instance.download_file(tg_file.file_path, destination=banner_path)
                    cta_asset = banner_path
                    logger.info("cta_loaded_from_telegram_file_id", user_id=user_id)
                except Exception as e:
                    logger.warning("cta_telegram_file_id_load_failed", error=str(e)[:200])
            if cta_asset is None and s is not None and s.cta_asset_path:
                p = Path(s.cta_asset_path)
                if p.exists():
                    cta_asset = p
        if cta_enabled and cta_asset is None:
            from app.services.overlays.cta_generator import ensure_cta_asset
            cta_asset, _ = ensure_cta_asset("", job_dir)
            logger.info("cta_default_asset_generated", path=str(cta_asset))

    # Run QuickPrep
    pipeline = QuickPrepPipeline()
    try:
        result = await pipeline.run(
            input_video=input_path,
            job_dir=job_dir,
            target_width=settings.output_width,
            target_height=settings.output_height,
            target_fps=settings.output_fps,
            video_bitrate=settings.output_video_bitrate,
            audio_bitrate=settings.output_audio_bitrate,
            cta_asset=cta_asset if cta_enabled else None,
            cta_position=cta_position,
            cta_mode=cta_mode,
            cta_duration_seconds=cta_duration_seconds,
            cta_start_seconds=cta_start_seconds,
            cta_min_margin_px=settings.cta_min_margin_px,
            output_width=settings.output_width,
            output_height=settings.output_height,
        )
    except Exception as e:
        logger.error("quickprep_failed", user_id=user_id, job_id=pending.job_id, error=str(e)[:200])
        await _fail_job(pending.job_id, "QUICKPREP_FAILED", str(e)[:500])
        await _edit_status(call.message, f"❌ Не удалось подготовить видео.\n\nОшибка в логах.")
        # Clean up the job workspace
        try:
            get_temp_manager().cleanup_job(job_dir.name)
        except Exception:
            pass
        _pop_pending(user_id)
        return

    await _edit_status(
        call.message,
        f"✅ Готово. Отправляю…\n\n"
        f"📦 {result.size_bytes // 1024 // 1024} МБ · {result.width}×{result.height}",
    )

    # Send via Telegram
    sender = TelegramSender(call.bot)
    from app.pipeline.final_renderer import FinalClip, FinalJob

    final_clip = FinalClip(
        index=1,
        final_path=result.final_path,
        has_subtitles=False,  # QuickPrep does not run Whisper
        has_cta=result.has_cta,
        size_bytes=result.size_bytes,
    )
    send_result = await sender.send(
        final_job=FinalJob(clips=(final_clip,)),
        chat_id=pending.chat_id,
        reply_to_message_id=pending.status_message_id,
    )

    if send_result.sent:
        await _edit_status(
            call.message,
            f"✅ Готово. Исходник удалён.\n\n🆔 Job #{pending.job_id}",
        )
        # Mark job completed
        async with db_manager.session() as session:
            repo = JobRepository(session)
            await repo.mark_completed(pending.job_id, clips_generated=1)
        _pop_pending(user_id)
    else:
        await _fail_job(pending.job_id, "SEND_FAILED")
        await _edit_status(
            call.message,
            f"❌ Не удалось отправить видео.\n\n🆔 Job #{pending.job_id}",
        )
        _pop_pending(user_id)

    # Always clean up
    try:
        get_temp_manager().cleanup_job(job_dir.name)
    except Exception:
        pass


@router.callback_query(F.data == "action:analyze_long")
async def on_analyze_long(call: CallbackQuery) -> None:
    """For long videos — full pipeline. Currently delegated to the legacy
    video handler flow. We reuse `_pending_jobs` data + the long pipeline
    runner we already have.
    """
    user_id = call.from_user.id if call.from_user else 0
    pending = _pop_pending(user_id)
    if pending is None:
        await call.answer("⚠️ Сначала отправь видео.", show_alert=True)
        return
    await call.answer("🔍 Анализирую…")

    # Run the long pipeline (legacy handler) in-place.
    # We import the inner function to avoid a circular import.
    from app.bot.handlers.video_legacy import run_long_pipeline
    try:
        await run_long_pipeline(
            call.message,
            call.bot,
            pending,
            user_id,
        )
    finally:
        # The legacy runner cleans up its own workspace.
        pass


@router.callback_query(F.data == "url:original")
async def on_url_original(call: CallbackQuery) -> None:
    """Send the downloaded URL video as-is, without Recut processing."""
    user_id = call.from_user.id if call.from_user else 0
    pending = _pop_pending(user_id)
    if pending is None:
        await call.answer("⚠️ Сначала отправь ссылку.", show_alert=True)
        return
    await call.answer("📥 Отправляю оригинал…")
    path = Path(pending.input_path)
    if not path.exists():
        await _fail_job(pending.job_id, "FILE_GONE")
        await call.message.edit_text("❌ Исходник не найден.")
        return
    # Send the actual downloaded video file, not the status message.
    try:
        await call.message.answer_video(
            video=types.FSInputFile(path),
            caption="Оригинал",
        )
    except Exception:
        # Fall back to document if Telegram rejects it as video.
        await call.message.answer_document(
            document=types.FSInputFile(path),
            caption="Оригинал",
        )
    async with db_manager.session() as session:
        repo = JobRepository(session)
        await repo.mark_completed(pending.job_id, clips_generated=0)
    await _safe_edit_text(
        call.message,
        "✅ Готово. Исходник удалён.",
    )
    try:
        get_temp_manager().cleanup_job(Path(pending.job_dir).name)
    except Exception:
        pass


@router.callback_query(F.data == "url:recut")
async def on_url_recut(call: CallbackQuery) -> None:
    """Run Recut QuickPrep on the downloaded URL video."""
    user_id = call.from_user.id if call.from_user else 0
    pending = _get_pending(user_id)
    if pending is None:
        await call.answer("⚠️ Сначала отправь ссылку.", show_alert=True)
        return
    await call.answer("🚀 Запускаю Recut…")
    settings = get_settings()
    input_path = Path(pending.input_path)
    job_dir = Path(pending.job_dir)
    await _edit_status(call.message, "🎬 Подготавливаю…\n\n⏳ FFmpeg в работе…")
    cta_asset: Path | None = None
    cta_enabled = False
    cta_position = "bottom"
    cta_mode = "end"
    cta_duration_seconds = 4.0
    cta_start_seconds = 0.0
    async with db_manager.session() as session:
        srepo = UserSettingsRepository(session)
        s = await srepo.get(user_id)
        if s is not None:
            cta_enabled = s.cta_enabled
            cta_position = s.cta_position
            cta_mode = s.cta_mode
            cta_duration_seconds = s.cta_duration_seconds
            cta_start_seconds = s.cta_start_seconds

        # Resolve CTA asset: prefer telegram_file_id (Railway-safe),
        # then local path, then default static banner.
        if cta_enabled:
            bot_instance = call.bot
            if s is not None and s.cta_telegram_file_id:
                try:
                    tg_file = await bot_instance.get_file(s.cta_telegram_file_id)
                    banner_path = job_dir / "cta_user.png"
                    await bot_instance.download_file(tg_file.file_path, destination=banner_path)
                    cta_asset = banner_path
                    logger.info("cta_loaded_from_telegram_file_id", user_id=user_id)
                except Exception as e:
                    logger.warning("cta_telegram_file_id_load_failed", error=str(e)[:200])
            if cta_asset is None and s is not None and s.cta_asset_path:
                p = Path(s.cta_asset_path)
                if p.exists():
                    cta_asset = p
        if cta_enabled and cta_asset is None:
            from app.services.overlays.cta_generator import ensure_cta_asset
            cta_asset, _ = ensure_cta_asset("", job_dir)
            logger.info("cta_default_asset_generated", path=str(cta_asset))
    pipeline = QuickPrepPipeline()
    try:
        result = await pipeline.run(
            input_video=input_path,
            job_dir=job_dir,
            target_width=settings.output_width,
            target_height=settings.output_height,
            target_fps=settings.output_fps,
            video_bitrate=settings.output_video_bitrate,
            audio_bitrate=settings.output_audio_bitrate,
            cta_asset=cta_asset if cta_enabled else None,
            cta_position=cta_position,
            cta_mode=cta_mode,
            cta_duration_seconds=cta_duration_seconds,
            cta_start_seconds=cta_start_seconds,
            cta_min_margin_px=settings.cta_min_margin_px,
            output_width=settings.output_width,
            output_height=settings.output_height,
        )
    except Exception as e:
        logger.error("quickprep_failed", user_id=user_id, job_id=pending.job_id, error=str(e)[:200])
        await _fail_job(pending.job_id, "QUICKPREP_FAILED", str(e)[:500])
        await _edit_status(call.message, f"❌ Не удалось подготовить видео.\n\nОшибка в логах.")
        try:
            get_temp_manager().cleanup_job(job_dir.name)
        except Exception:
            pass
        return
    await _edit_status(
        call.message,
        f"✅ Готово. Отправляю…\n\n"
        f"📦 {result.size_bytes // 1024 // 1024} МБ · {result.width}×{result.height}",
    )
    sender = TelegramSender(call.bot)
    from app.pipeline.final_renderer import FinalClip, FinalJob
    final_clip = FinalClip(
        index=1,
        final_path=result.final_path,
        has_subtitles=False,
        has_cta=result.has_cta,
        size_bytes=result.size_bytes,
    )
    send_result = await sender.send(
        final_job=FinalJob(clips=(final_clip,)),
        chat_id=pending.chat_id,
        reply_to_message_id=pending.status_message_id,
    )
    if send_result.sent:
        await _edit_status(
            call.message,
            "✅ Готово. Исходник удалён.\n\n🆔 Job #" + str(pending.job_id),
        )
        async with db_manager.session() as session:
            repo = JobRepository(session)
            await repo.mark_completed(pending.job_id, clips_generated=1)
    else:
        await _edit_status(
            call.message,
            "❌ Не удалось отправить видео.\n\n🆔 Job #" + str(pending.job_id),
        )
    try:
        get_temp_manager().cleanup_job(job_dir.name)
    except Exception:
        pass


@router.callback_query(F.data == "action:settings")
async def on_settings(call: CallbackQuery) -> None:
    await _show_settings(call.message, call.from_user.id if call.from_user else 0)
    await call.answer()


@router.callback_query(F.data == "settings:back")
async def on_settings_back(call: CallbackQuery) -> None:
    user_id = call.from_user.id if call.from_user else 0
    pending = _pending_jobs.get(user_id)
    if pending is not None:
        # Restore the main action menu for the pending job
        input_path = Path(pending.input_path)
        if input_path.exists():
            actual_size = input_path.stat().st_size
            await call.message.edit_text(
                f"✅ Видео загружено ({actual_size // 1024 // 1024} МБ).\n\nВыбери действие:",
                reply_markup=ACTION_MENU,
            )
            await call.answer()
            return
    # No pending job — go back to /start
    await call.message.edit_text(
        "🎬 <b>Recut</b>\n\nОтправь видео — подготовлю его к публикации.",
        parse_mode="HTML",
    )
    await call.answer()


@router.callback_query(F.data == "settings:toggle_cta")
async def on_toggle_cta(call: CallbackQuery) -> None:
    user_id = call.from_user.id if call.from_user else 0
    async with db_manager.session() as session:
        s = await UserSettingsRepository(session).update_fields(user_id)
        s.cta_enabled = not s.cta_enabled
    await _show_settings(call.message, user_id)
    await call.answer(f"CTA {'включён' if s.cta_enabled else 'выключен'}")


@router.callback_query(F.data == "settings:toggle_subs")
async def on_toggle_subs(call: CallbackQuery) -> None:
    user_id = call.from_user.id if call.from_user else 0
    async with db_manager.session() as session:
        s = await UserSettingsRepository(session).update_fields(user_id)
        s.subtitles_enabled = not s.subtitles_enabled
    await _show_settings(call.message, user_id)
    await call.answer(f"Субтитры {'включены' if s.subtitles_enabled else 'выключены'}")


@router.callback_query(F.data == "settings:position")
async def on_settings_position(call: CallbackQuery) -> None:
    await call.message.edit_text("📍 Выбери позицию CTA:", reply_markup=POSITION_MENU)
    await call.answer()


@router.callback_query(F.data == "settings:timing")
async def on_settings_timing(call: CallbackQuery) -> None:
    await call.message.edit_text("⏱ Когда показывать CTA?", reply_markup=TIMING_MENU)
    await call.answer()


@router.callback_query(F.data == "settings:upload_cta")
async def on_settings_upload_cta(call: CallbackQuery) -> None:
    await call.message.edit_text(
        "📎 Отправь мне свой баннер (PNG с прозрачностью).\n\n"
        "Я сохраню его и буду использовать при подготовке видео.\n\n"
        "/cancel — отмена"
    )
    await call.answer()
    # Mark user as waiting for banner upload
    _awaiting_banner.add(call.from_user.id if call.from_user else 0)


_awaiting_banner: set[int] = set()


@router.callback_query(F.data.startswith("cta_pos:"))
async def on_set_position(call: CallbackQuery) -> None:
    pos = call.data.split(":", 1)[1]
    user_id = call.from_user.id if call.from_user else 0
    if pos not in CTA_POSITIONS:
        await call.answer("Неизвестная позиция")
        return
    async with db_manager.session() as session:
        await UserSettingsRepository(session).update_fields(
            user_id, cta_position=pos,
        )
    await call.message.edit_text(
        f"✅ Позиция сохранена: {CTA_POSITIONS[pos]}\n\n"
        f"Хочешь посмотреть как будет выглядеть?",
        reply_markup=preview_keyboard(),
    )
    await call.answer()


@router.callback_query(F.data.startswith("cta_time:"))
async def on_set_timing(call: CallbackQuery) -> None:
    key = call.data.split(":", 1)[1]
    user_id = call.from_user.id if call.from_user else 0
    mapping = {
        "full": ("full", 0.0),
        "start_3": ("start", 3.0),
        "end_3": ("end", 3.0),
        "end_5": ("end", 5.0),
    }
    if key not in mapping:
        await call.answer("Неизвестный вариант")
        return
    mode, dur = mapping[key]
    async with db_manager.session() as session:
        await UserSettingsRepository(session).update_fields(
            user_id,
            cta_mode=mode,
            cta_duration_seconds=dur,
        )
    label = CTA_TIMING.get(key, key)
    await call.message.edit_text(
        f"✅ Время показа сохранено: {label}\n\n"
        f"Хочешь посмотреть как будет выглядеть?",
        reply_markup=preview_keyboard(),
    )
    await call.answer()


@router.callback_query(F.data == "preview:save")
async def on_preview_save(call: CallbackQuery) -> None:
    user_id = call.from_user.id if call.from_user else 0
    await _show_settings(call.message, user_id)
    await call.answer("✅ Сохранено")


# ---------------------------------------------------------------------------
# Banner upload handler (user sends PNG)
# ---------------------------------------------------------------------------

@router.message(F.document, F.document.mime_type == "image/png")
async def on_banner_upload(message: types.Message, bot: Bot) -> None:
    user_id = message.from_user.id if message.from_user else 0
    if user_id not in _awaiting_banner:
        return  # not waiting for a banner
    _awaiting_banner.discard(user_id)

    doc = message.document
    if not doc or not doc.file_id:
        await message.answer("❌ Не удалось получить файл.")
        return

    # Download temporarily, validate with Pillow, then store file_id in DB.
    try:
        file = await bot.get_file(doc.file_id)
        tmp_png = _USER_ASSETS_DIR / f"_tmp_banner_{user_id}.png"
        await bot.download_file(file.file_path, destination=tmp_png)
    except Exception as e:
        logger.error("banner_download_failed", user_id=user_id, error=str(e)[:200])
        await message.answer("❌ Не удалось скачать файл.")
        return

    # Validate with Pillow: must be PNG RGBA with non-zero dimensions.
    try:
        from PIL import Image
        img = Image.open(tmp_png)
        if img.format != "PNG":
            tmp_png.unlink(missing_ok=True)
            await message.answer("❌ Отправь плашку именно как PNG-файл.")
            return
        if img.mode not in ("RGBA", "RGB"):
            # Convert to RGBA for alpha support
            img = img.convert("RGBA")
        w, h = img.size
        if w <= 0 or h <= 0:
            tmp_png.unlink(missing_ok=True)
            await message.answer("❌ Изображение повреждено.")
            return
    except Exception:
        tmp_png.unlink(missing_ok=True)
        await message.answer("❌ Не удалось прочитать изображение. Отправь PNG-файл.")
        return

    # Store Telegram file_id in DB (survives Railway redeploy).
    async with db_manager.session() as session:
        await UserSettingsRepository(session).update_fields(
            user_id,
            cta_telegram_file_id=doc.file_id,
            cta_enabled=True,
        )
    tmp_png.unlink(missing_ok=True)

    await message.answer(
        "✅ Плашка сохранена\n\n"
        "Открой /start → ⚙️ Настройки чтобы выбрать позицию и время показа."
    )


# ---------------------------------------------------------------------------
# Helpers
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


async def _show_settings(message: types.Message, user_id: int) -> None:
    """Render the Settings menu based on current DB state."""
    async with db_manager.session() as session:
        s = await UserSettingsRepository(session).get_or_create(user_id)

    pos = CTA_POSITIONS.get(s.cta_position, s.cta_position)
    timing = (
        f"Весь ролик" if s.cta_mode == "full"
        else f"Последние {s.cta_duration_seconds:g} сек" if s.cta_mode == "end"
        else f"Первые {s.cta_duration_seconds:g} сек" if s.cta_mode == "start"
        else f"С {s.cta_start_seconds:g}с ({s.cta_duration_seconds:g}с)"
    )
    banner = "✅ Загружен" if s.cta_asset_path else "❌ Не загружен"

    text = (
        f"⚙️ <b>Настройки</b>\n\n"
        f"CTA: {'✅ ВКЛ' if s.cta_enabled else '❌ ВЫКЛ'}\n"
        f"Баннер: {banner}\n"
        f"Позиция: {pos}\n"
        f"Время: {timing}\n"
        f"Субтитры: {'✅ ВКЛ' if s.subtitles_enabled else '❌ ВЫКЛ'}\n\n"
        f"Формат вывода: 9:16 ({get_settings().output_width}×{get_settings().output_height})"
    )
    await _safe_edit_text(message, text, reply_markup=SETTINGS_MENU, parse_mode="HTML")


async def _safe_edit_text(
    message: types.Message, text: str, reply_markup=None, parse_mode: str | None = None,
) -> None:
    """Edit a message, silently ignoring Telegram's "not modified" error.

    Telegram rejects edit_text() when the new content is identical to the
    current one. That happens legitimately when the user taps a button
    multiple times — the second tap is a no-op for them, but our handler
    must not crash.
    """
    try:
        await message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except Exception as e:
        # Best-effort: log and move on. TelegramBadRequest("not modified")
        # is the common case; we don't want to spam logs with it.
        err = str(e)
        if "not modified" in err:
            return  # benign — user tapped the same button twice
        logger.warning("edit_text_failed", error=err[:120])


async def _edit_status(message: types.Message, text: str) -> None:
    """Edit a status message. We swallow Bad Request in case the original
    message was deleted or is too old for edits.
    """
    try:
        await message.edit_text(text)
    except Exception as e:
        logger.warning("status_edit_failed", error=str(e)[:120])