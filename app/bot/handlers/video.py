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
import io
import shutil
import tempfile
import uuid
from pathlib import Path

from aiogram import Bot, F, Router, types
from aiogram.types import CallbackQuery

from app.bot.keyboards.inline import (
    BANNER_CANCEL_MENU,
    HOME_MENU,
    MORE_MENU,
    POSITION_MENU,
    RESULT_MENU_MOMENTS,
    RESULT_MENU_PREPARE,
    RESULT_MENU_VERSIONS,
    SIZE_MENU,
    TIMING_MENU,
    appearance_menu,
    background_menu,
    banner_menu,
    fine_menu,
    mode_input_menu,
    audio_menu,
    preview_keyboard,
    style_pick_menu,
    title_menu,
)
from app.core.config import get_settings
from app.services.overlays.templates import BACKGROUNDS, TITLES
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

@router.message(F.video | F.video_note | F.animation | F.document)
async def on_video_message(message: types.Message, bot: Bot) -> None:
    user_id: int = message.from_user.id if message.from_user else 0
    if not user_id:
        return

    settings = get_settings()

    # Banner upload state takes priority: a document while waiting for a
    # banner asset is the banner, not a video (HOTFIX routing fix).
    doc_only = message.document and not (message.video or message.video_note or message.animation)
    if doc_only and user_id in _awaiting_banner:
        await _handle_banner_asset(message, bot)
        return

    # Detect attachment
    attachment = _pick_attachment(message)
    if attachment is None:
        return

    declared_size = int(getattr(attachment, "file_size", 0) or 0)
    mime = getattr(attachment, "mime_type", None)
    filename = getattr(attachment, "file_name", None) or ""

    # Telegram Bot API limit: 20 MB for files via getFile.
    TELEGRAM_BOT_API_LIMIT = 20 * 1024 * 1024
    if declared_size > TELEGRAM_BOT_API_LIMIT:
        await message.answer(
            "❌ Файл слишком большой для загрузки через текущий Telegram API.\n\n"
            "Пришли ссылку или файл меньшего размера."
        )
        return

    # MIME is only a HINT (HOTFIX items 5/6). A clearly non-video document
    # is rejected early; octet-stream/None/video/* all proceed to ffprobe.
    if mime and not (mime.startswith("video/") or mime == "application/octet-stream"
                     or mime.startswith("application/x-matroska")):
        # Cheap filename check before rejecting — mislabeled MP4s happen.
        _vid_exts = (".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".mpg",
                     ".mpeg", ".ts", ".wmv", ".flv", ".3gp")
        if not filename.lower().endswith(_vid_exts):
            await message.answer(
                f"❌ Это не видео-файл ({mime}).\n\n"
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
            source_filename=filename or "video",
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
            source=message, job_dir=job_dir, filename=None,
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

    # ffprobe decides (HOTFIX item 6) — MIME was only a hint.
    try:
        probe = get_probe_service()
        meta = await probe.probe(input_path)
        duration_sec = meta.duration_seconds
        w, h = meta.width, meta.height
        if duration_sec <= 0 or w <= 0 or h <= 0:
            raise ValueError("no video stream / zero duration")
    except Exception as e:
        logger.warning("local_video_ffprobe_rejected", user_id=user_id,
                       job_id=job_id, error=str(e)[:200])
        await _fail_job(job_id, "NOT_A_VIDEO", str(e)[:300])
        await status_msg.edit_text(
            "❌ Это не видео (или файл повреждён).\n\n"
            "Отправь MP4 / MOV / MKV / WebM — или ссылку на ролик."
        )
        temp.cleanup_job(job_dir.name)
        return

    # HOTFIX order (item 8): persist DB state BEFORE touching the Telegram UI.
    # 1) Job READY — guarded so an enum/DB error can't hang the bot silently.
    from app.database.models import JobStatus
    try:
        async with db_manager.session() as session:
            repo = JobRepository(session)
            await repo.set_status(
                job_id=job_id,
                status=JobStatus.READY,
                status_message_id=status_msg.message_id,
            )
            from sqlalchemy import update as _u
            from app.database.models import Job as _Job
            await session.execute(
                _u(_Job).where(_Job.id == job_id).values(source_bytes=actual_size)
            )
    except Exception:
        logger.exception("job_ready_transition_failed", job_id=job_id)
        await _fail_job(job_id, "MEDIA_READY_FAILED", "READY persist failed")
        await status_msg.edit_text(
            "❌ Не удалось сохранить видео.\nКод ошибки: MEDIA_READY_FAILED"
        )
        temp.cleanup_job(job_dir.name)
        return

    # 2) Save CURRENT source separately from render workspace (item 6).
    current_dir = Path(f"/tmp/recut/current/{user_id}")
    current_dir.mkdir(parents=True, exist_ok=True)
    current_ext = input_path.suffix or ".mp4"
    current_path = current_dir / f"source{current_ext}"
    import shutil
    shutil.copy2(str(input_path), str(current_path))
    telegram_file_id = getattr(attachment, "file_id", None)
    try:
        from app.services.current_media import get_current_media_service
        await get_current_media_service().set_ready(
            user_id, current_path, job_id=job_id,
            telegram_file_id=telegram_file_id,
        )
    except Exception:
        logger.exception("current_media_set_failed", job_id=job_id)

    # 3) In-memory pending cache.
    _pending_jobs[user_id] = _PendingJob(
        job_id=job_id,
        chat_id=message.chat.id,
        input_path=str(input_path),
        job_dir=str(job_dir),
        status_message_id=status_msg.message_id,
    )

    # 4) Only now render the UI — a UI failure can no longer lose media.
    dur_str = _fmt_duration(duration_sec) if duration_sec > 0 else "—"
    res_str = f"{w}×{h}" if w > 0 and h > 0 else "—"
    text = (
        f"🎬 <b>Видео готово</b>\n\n"
        f"⏱ {dur_str}\n"
        f"📐 {res_str}\n\n"
        f"Выбери действие:"
    )
    # Decide menu: chosen mode wins; long videos get Smart Clips option.
    SMART_CLIPS_MIN_SECONDS = 120
    selected_mode = get_selected_mode(user_id)
    if selected_mode:
        menu = mode_input_menu(selected_mode)
        _mode_state.pop(user_id, None)
    else:
        menu = mode_input_menu("moments" if duration_sec >= SMART_CLIPS_MIN_SECONDS else "prepare")
    await _safe_edit_text(status_msg, text, reply_markup=menu, parse_mode="HTML")


@router.message(F.text)
async def on_url_message(message: types.Message, bot: Bot) -> None:
    """Accept a supported video URL, download, then show actions."""
    user_id: int = message.from_user.id if message.from_user else 0
    if not user_id:
        return
    # 1. Active input states take priority
    if user_id in _awaiting_title:
        _awaiting_title.discard(user_id)
        text_input = (message.text or "").strip()
        if not text_input:
            await message.answer("❌ Пустой текст. Попробуй ещё раз.")
            return
        async with db_manager.session() as session:
            await UserSettingsRepository(session).update_fields(
                user_id, title_id="custom", custom_title=text_input[:100],
            )
        await message.answer(f"✅ Заголовок сохранён: «{text_input[:100]}»")
        return
    if user_id in _awaiting_banner:
        await message.answer(
            "📎 Жду файл плашки: PNG, JPG, WebP, GIF или короткий MP4.",
            reply_markup=BANNER_CANCEL_MENU,
        )
        return
    text = (message.text or "").strip()
    # HOTFIX items 9/10/11: extract → normalize → ONE validator → queue.
    from app.services.downloader.url_utils import extract_url, normalize_url, get_platform_name
    url = extract_url(text)
    if url is None:
        return
    url = normalize_url(url)
    platform = get_platform_name(url)
    from app.services.downloader.jobs import get_job_queue
    queue = get_job_queue()
    if queue.is_busy(user_id):
        await message.answer(
            "⏳ Сейчас обрабатываю видео. Жди завершения или отправь новое после.")
        return
    svc = DownloaderService()
    try:
        svc._validate(url)
    except Exception:
        await message.answer(
            "❌ Поддерживаются ссылки: TikTok, Instagram, YouTube. Попробуй другую.")
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

    logger.info(
        "download_request", user_id=user_id, job_id=job_id,
        normalized_url=url, platform=platform, raw_text_len=len(text),
    )

    # HOTFIX item 11: run_download (semaphore + per-user lock + inflight dedup).
    async def _download_work():
        return await svc.download(url, job_dir)

    try:
        result = await queue.run_download(user_id, url, _download_work)
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

    # Probe downloaded file for UX metadata (same UI as Telegram upload).
    try:
        probe = get_probe_service()
        meta = await probe.probe(result.path)
        duration_sec = meta.duration_seconds
        w, h = meta.width, meta.height
        logger.info("url_probe_ok", user_id=user_id, job_id=job_id, w=w, h=h, duration=duration_sec)
    except Exception as e:
        logger.warning("url_probe_failed_fallback", user_id=user_id, job_id=job_id, error=str(e)[:120])
        duration_sec = result.duration_seconds
        w, h = 0, 0

    actual_size = result.size_bytes
    # HOTFIX item 8: DB/persist FIRST, UI last. READY transition guarded.
    from app.database.models import JobStatus
    try:
        async with db_manager.session() as session:
            repo = JobRepository(session)
            await repo.set_status(
                job_id=job_id,
                status=JobStatus.READY,
                status_message_id=status_msg.message_id,
            )
            from sqlalchemy import update as _u
            from app.database.models import Job as _Job
            await session.execute(
                _u(_Job).where(_Job.id == job_id).values(source_bytes=actual_size)
            )
    except Exception:
        logger.exception("job_ready_transition_failed", job_id=job_id, source="url")
        await _fail_job(job_id, "MEDIA_READY_FAILED", "READY persist failed (url)")
        await status_msg.edit_text(
            "❌ Не удалось сохранить видео.\nКод ошибки: MEDIA_READY_FAILED"
        )
        temp.cleanup_job(job_dir.name)
        return

    try:
        from app.services.current_media import get_current_media_service
        await get_current_media_service().set_ready(
            user_id, Path(result.path), job_id=job_id, source_url=url)
    except Exception:
        logger.exception("current_media_set_failed", job_id=job_id)

    _pending_jobs[user_id] = _PendingJob(
        job_id=job_id,
        chat_id=message.chat.id,
        input_path=str(result.path),
        job_dir=str(job_dir),
        status_message_id=status_msg.message_id,
    )

    dur_str = _fmt_duration(duration_sec) if duration_sec > 0 else "—"
    res_str = f"{w}×{h}" if w > 0 and h > 0 else "—"
    text_out = f"🎬 <b>Видео готово</b>\n\n"
    if duration_sec > 0:
        text_out += f"⏱ {dur_str}\n"
    if w > 0 and h > 0:
        text_out += f"📐 {res_str}\n"
    text_out += "\nВыбери действие:"

    SMART_CLIPS_MIN_SECONDS = 120
    selected_mode = get_selected_mode(user_id)
    if selected_mode:
        menu = mode_input_menu(selected_mode)
        _mode_state.pop(user_id, None)
    else:
        menu = mode_input_menu("moments" if duration_sec >= SMART_CLIPS_MIN_SECONDS else "prepare")
    logger.info("url_ready_for_actions", user_id=user_id, job_id=job_id, menu=type(menu).__name__)
    await _safe_edit_text(status_msg, text_out, reply_markup=menu, parse_mode="HTML")


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
        # Phase 1 recovery (audit #14/51): reuse current media from DB
        # instead of asking the user to re-send the video.
        try:
            from app.services.current_media import get_current_media_service
            cm = await get_current_media_service().get(user_id)
            if cm is not None and cm.source_path and cm.source_path.exists():
                pass  # local file alive
            elif cm is not None and (cm.telegram_file_id or cm.source_url):
                # TZ Phase 24: /tmp is ephemeral on Railway — re-download.
                redl = Path(tempfile.gettempdir()) / f"redl_{user_id}.mp4"
                try:
                    if cm.telegram_file_id and call.message is not None:
                        tg = await call.message.bot.get_file(cm.telegram_file_id)
                        await call.message.bot.download_file(tg.file_path, str(redl))
                    elif cm.source_url:
                                            from app.pipeline.url_downloader import DownloaderService
                                            await DownloaderService().download(
                                                cm.source_url, redl)
                    if redl.exists():
                        await get_current_media_service().set_ready(
                            user_id, redl, job_id=cm.job_id,
                            telegram_file_id=cm.telegram_file_id,
                            source_url=cm.source_url)
                        cm = await get_current_media_service().get(user_id)
                except Exception as e2:
                    logger.warning("current_media_redownload_failed", error=str(e2)[:150])
            if cm is not None and cm.source_path and cm.source_path.exists():
                pending = _PendingJob(
                    job_id=cm.job_id or 0,
                    chat_id=call.message.chat.id if call.message else 0,
                    input_path=str(cm.source_path),
                    job_dir=str(cm.source_path.parent),
                    status_message_id=call.message.message_id if call.message else 0,
                )
                _pending_jobs[user_id] = pending
        except Exception as e:
            logger.warning("current_media_reuse_failed", error=str(e)[:150])
    if pending is None:
        await call.answer("⚠️ Сначала отправь видео.", show_alert=True)
        return
    await call.answer("🚀 Запускаю…")

    settings = get_settings()
    input_path = Path(pending.input_path)
    job_dir = Path(pending.job_dir)

    await _edit_status(call.message, "🎬 Подготавливаю…\n\n⏳ FFmpeg в работе…")

    try:
        # Load user's CTA settings
        cta_asset: Path | None = None
        cta_enabled = False
        cta_position = "bottom"
        cta_mode = "end"
        cta_duration_seconds = 4.0
        cta_start_seconds = 0.0
        cta_size = "medium"
        overlay_type = "png"

        async with db_manager.session() as session:
            srepo = UserSettingsRepository(session)
            s = await srepo.get(user_id)
            if s is not None:
                cta_enabled = s.cta_enabled
                cta_position = s.cta_position
                cta_mode = s.cta_mode
                cta_duration_seconds = s.cta_duration_seconds
                cta_start_seconds = s.cta_start_seconds
                cta_size = getattr(s, "cta_size", None) or "medium"
                overlay_type = getattr(s, "overlay_type", None) or "png"

            # Resolve CTA asset: ONLY the user's banner (telegram_file_id or
            # local path). No default "Recut" placeholder in production:
            # no banner → no overlay.
            if cta_enabled:
                bot_instance = call.bot
                if s is not None and s.cta_telegram_file_id:
                    try:
                        tg_file = await bot_instance.get_file(s.cta_telegram_file_id)
                        ext = {"png": "png", "webp": "webp", "gif": "gif", "mp4": "mp4"}.get(overlay_type, "png")
                        banner_path = job_dir / f"cta_user.{ext}"
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
                logger.info("cta_no_user_banner_skipping_overlay", user_id=user_id)

        # Run QuickPrep
        pipeline = QuickPrepPipeline()
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
            cta_size_preset=cta_size,
            cta_overlay_type=overlay_type,
            background_id=getattr(s, "background_id", "blur") if s else "blur",
            title_text=_resolve_title_text(s),
            brand_corner=bool(getattr(s, "brand_corner", False)) if s else False,
            audio_preset=getattr(s, "audio_preset", "original") if s else "original",
            transformation_preset=getattr(s, "style_id", "custom") if s else "custom",
        )

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
                reply_markup=RESULT_MENU_PREPARE,
            )
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

    except Exception as e:
        logger.exception("quickprep_pipeline_failed", user_id=user_id, job_id=pending.job_id)
        await _fail_job(pending.job_id, "QUICKPREP_FAILED", str(e)[:500])
        await _edit_status(call.message, "❌ Не удалось подготовить видео.")
        _pop_pending(user_id)
    finally:
        try:
            get_temp_manager().cleanup_job(job_dir.name)
        except Exception:
            pass


@router.callback_query(F.data == "action:versions")
async def on_action_versions(call: CallbackQuery) -> None:
    """STEP 5: ✨ Сделать 3 версии — three real edit decisions."""
    user_id = call.from_user.id if call.from_user else 0
    pending = _get_pending(user_id)
    if pending is None:
        await call.answer("⚠️ Сначала отправь видео.", show_alert=True)
        return
    await call.answer("✨ Готовлю 3 версии…")

    input_path = Path(pending.input_path)
    job_dir = Path(pending.job_dir)

    await _edit_status(call.message, "✨ Монтирую 3 версии…\n\n⏳ Это займёт минуту-две.")

    try:
        # CTA settings (same resolution as quick_prep).
        cta_asset: Path | None = None
        cta_enabled = False
        cta_position = "bottom"
        async with db_manager.session() as session:
            s = await UserSettingsRepository(session).get(user_id)
            if s is not None:
                cta_enabled = s.cta_enabled
                cta_position = s.cta_position
            if cta_enabled:
                if s is not None and s.cta_telegram_file_id:
                    try:
                        tg_file = await call.bot.get_file(s.cta_telegram_file_id)
                        banner_path = job_dir / "cta_user.png"
                        await call.bot.download_file(tg_file.file_path, destination=banner_path)
                        cta_asset = banner_path
                    except Exception as e:
                        logger.warning("cta_telegram_file_id_load_failed", error=str(e)[:200])
                if cta_asset is None and s is not None and s.cta_asset_path:
                    p = Path(s.cta_asset_path)
                    if p.exists():
                        cta_asset = p

        from app.pipeline.three_versions import ThreeVersionsPipeline
        pipeline = ThreeVersionsPipeline()
        results = await pipeline.run(
            input_path, job_dir,
            cta_asset=cta_asset if cta_enabled else None,
            cta_position=cta_position,
            cta_margin_px=0,  # auto ~9.5% of height
        )

        await _edit_status(call.message, "✅ Готово. Отправляю 3 варианта…")

        sender = TelegramSender(call.bot)
        from app.pipeline.final_renderer import FinalClip, FinalJob
        clips = tuple(
            FinalClip(
                index=i + 1,
                final_path=r.final_path,
                has_subtitles=False,
                has_cta=False,
                size_bytes=r.size_bytes,
            )
            for i, r in enumerate(results)
        )
        send_result = await sender.send(
            final_job=FinalJob(clips=clips),
            chat_id=pending.chat_id,
            reply_to_message_id=pending.status_message_id,
        )

        if send_result.sent:
            await _edit_status(
                call.message,
                f"✅ Готово 3 варианта.",
                reply_markup=RESULT_MENU_VERSIONS,
            )
            async with db_manager.session() as session:
                repo = JobRepository(session)
                await repo.mark_completed(pending.job_id, clips_generated=3)
            _pop_pending(user_id)
        else:
            await _fail_job(pending.job_id, "SEND_FAILED")
            await _edit_status(call.message, "❌ Не удалось отправить видео.")
            _pop_pending(user_id)

    except Exception as e:
        logger.exception("versions_pipeline_failed", user_id=user_id, job_id=pending.job_id)
        await _fail_job(pending.job_id, "VERSIONS_FAILED", str(e)[:500])
        await _edit_status(call.message, "❌ Не удалось сделать 3 версии.")
        _pop_pending(user_id)
    finally:
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
        reply_markup=RESULT_MENU_PREPARE,
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
    cta_size = "medium"
    overlay_type = "png"
    async with db_manager.session() as session:
        srepo = UserSettingsRepository(session)
        s = await srepo.get(user_id)
        if s is not None:
            cta_enabled = s.cta_enabled
            cta_position = s.cta_position
            cta_mode = s.cta_mode
            cta_duration_seconds = s.cta_duration_seconds
            cta_start_seconds = s.cta_start_seconds
            cta_size = getattr(s, "cta_size", None) or "medium"
            overlay_type = getattr(s, "overlay_type", None) or "png"

        # Resolve CTA asset: prefer telegram_file_id (Railway-safe),
        # then local path, then default static banner.
        if cta_enabled:
            bot_instance = call.bot
            if s is not None and s.cta_telegram_file_id:
                try:
                    tg_file = await bot_instance.get_file(s.cta_telegram_file_id)
                    ext = {"png": "png", "webp": "webp", "gif": "gif", "mp4": "mp4"}.get(overlay_type, "png")
                    banner_path = job_dir / f"cta_user.{ext}"
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
            logger.info("cta_no_user_banner_skipping_overlay", user_id=user_id)
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
            cta_size_preset=cta_size,
            cta_overlay_type=overlay_type,
            background_id=getattr(s, "background_id", "blur") if s else "blur",
            title_text=_resolve_title_text(s),
            brand_corner=bool(getattr(s, "brand_corner", False)) if s else False,
            audio_preset=getattr(s, "audio_preset", "original") if s else "original",
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
            reply_markup=RESULT_MENU_PREPARE,
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
    # Legacy alias -> Тонкая настройка (PART 18: no duplicate CTA settings).
    await on_fine_menu(call)


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
                reply_markup=mode_input_menu("prepare"),
            )
            await call.answer()
            return
# No pending job — show HOME
    await call.message.edit_text(
        HOME_TEXT,
        parse_mode="HTML",
        reply_markup=HOME_MENU,
    )
    await call.answer()


# ---------------------------------------------------------------------------
# HOME / mode selection / banner section (audit #1, #3-9, #33)
# ---------------------------------------------------------------------------

HOME_TEXT = (
    "🎬 <b>ReCut</b>\n\nЧто сделать?"
)

_MODE_PROMPTS = {
    "prepare": (
        "🚀 <b>Подготовить к публикации</b>\n\n"
        "📎 Пришли видео или ссылку на TikTok / Reels / Shorts."
    ),
    "versions": (
        "✨ <b>Сделать 3 версии</b>\n\n"
        "📎 Пришли короткий ролик.\n\n"
        "Сделаю несколько разных монтажных вариантов."
    ),
    "moments": (
        "✂️ <b>Найти лучшие моменты</b>\n\n"
        "📎 Пришли длинное видео или ссылку."
    ),
}

# Selected mode per user; consumed when a video/URL arrives.
_mode_state: dict[int, str] = {}


@router.callback_query(F.data == "home:open")
async def on_home_open(call: CallbackQuery) -> None:
    _mode_state.pop(call.from_user.id if call.from_user else 0, None)
    await call.message.edit_text(HOME_TEXT, parse_mode="HTML", reply_markup=HOME_MENU)
    await call.answer()


@router.callback_query(F.data.startswith("mode:"))
async def on_mode_selected(call: CallbackQuery) -> None:
    user_id = call.from_user.id if call.from_user else 0
    mode = (call.data or "").split(":", 1)[1]
    if mode not in _MODE_PROMPTS:
        await call.answer("Неизвестный режим")
        return
    _mode_state[user_id] = mode
    await call.message.edit_text(_MODE_PROMPTS[mode], parse_mode="HTML")
    await call.answer()


def get_selected_mode(user_id: int) -> str | None:
    return _mode_state.get(user_id)


@router.callback_query(F.data == "banner:menu")
async def on_banner_menu(call: CallbackQuery) -> None:
    user_id = call.from_user.id if call.from_user else 0
    file_id = None
    try:
        async with db_manager.session() as session:
            s = await UserSettingsRepository(session).get_or_create(user_id)
            file_id = s.cta_telegram_file_id
    except Exception as e:
        logger.warning("banner_menu_db_failed", error=str(e)[:200])
    if file_id:
        text = (
            "🖼 <b>Плашка</b>\n\n"
            "Статус: ✅ Загружена\n"
            f"Положение: {getattr(s, 'cta_position', 'снизу')}\n"
            "Показ: последние сек."
        )
    else:
        text = "🖼 <b>Плашка</b>\n\nПлашка пока не загружена."
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=banner_menu(bool(file_id)))
    await call.answer()


@router.callback_query(F.data == "banner:upload")
async def on_banner_upload_request(call: CallbackQuery) -> None:
    user_id = call.from_user.id if call.from_user else 0
    _awaiting_banner.add(user_id)
    await call.message.edit_text(
        "📎 Пришли плашку: <b>PNG, JPEG, WebP, GIF или короткий MP4</b>.\n\n"
        "Важно: отправь её как <b>ФАЙЛ</b>, а не как фото —\n"
        "так сохранится качество и прозрачность.",
        parse_mode="HTML",
        reply_markup=BANNER_CANCEL_MENU,
    )
    await call.answer()


@router.callback_query(F.data == "banner:cancel")
async def on_banner_cancel(call: CallbackQuery) -> None:
    user_id = call.from_user.id if call.from_user else 0
    _awaiting_banner.discard(user_id)
    # BUG 8: read DB for existing banner state
    has_banner = False
    async with db_manager.session() as session:
        s = await UserSettingsRepository(session).get_or_create(user_id)
        has_banner = bool(s.cta_telegram_file_id)
    await call.message.edit_text("Отменено.", reply_markup=banner_menu(has_banner))
    await call.answer()


@router.callback_query(F.data == "banner:delete")
async def on_banner_delete(call: CallbackQuery) -> None:
    user_id = call.from_user.id if call.from_user else 0
    async with db_manager.session() as session:
        s = await UserSettingsRepository(session).update_fields(user_id)
        s.cta_telegram_file_id = None
        s.cta_enabled = False
    await call.message.edit_text(
        "🗑 Плашка удалена.",
        reply_markup=banner_menu(False),
    )
    await call.answer("Плашка удалена")


@router.callback_query(F.data == "banner:preview")
async def on_banner_preview(call: CallbackQuery) -> None:
    user_id = call.from_user.id if call.from_user else 0
    file_id = None
    async with db_manager.session() as session:
        s = await UserSettingsRepository(session).get_or_create(user_id)
        file_id = s.cta_telegram_file_id
    if not file_id:
        await call.answer("Плашка не загружена", show_alert=True)
        return
    try:
        bot = call.bot
        tg_file = await bot.get_file(file_id)
        buf = io.BytesIO()
        await bot.download_file(tg_file.file_path if hasattr(tg_file, "file_path") else tg_file, destination=buf)
    except Exception:
        await call.answer("Не удалось загрузить плашку", show_alert=True)
        return
    buf.seek(0)

    # PART 19: preview as a real 3s clip (same renderer as production) so
    # static AND animated overlays preview identically. Falls back to a
    # plain photo preview if rendering fails.
    try:
        tmp_dir = _USER_ASSETS_DIR / f"preview_{user_id}"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        ext = {"png": "png", "webp": "webp", "gif": "gif", "mp4": "mp4"}.get(
            getattr(s, "overlay_type", None) or "png", "png")
        asset_path = tmp_dir / f"banner.{ext}"
        asset_path.write_bytes(buf.getvalue())

        # Tiny color clip as the background canvas.
        import asyncio as _asyncio
        base = tmp_dir / "base.mp4"
        from app.services.media.ffmpeg import MediaService as _MS
        ms = _MS()
        cmd = [
            ms._ffmpeg_path, "-y", "-v", "error",
            "-f", "lavfi", "-i", f"color=c=#202028:s=540x960:r=30:d=3",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(base),
        ]
        proc = await _asyncio.create_subprocess_exec(*cmd)
        await _asyncio.wait_for(proc.communicate(), timeout=60)

        preview = tmp_dir / "preview.mp4"
        overlay_type = getattr(s, "overlay_type", None) or "png"
        await ms.burn_cta(
            base, asset_path, preview,
            position=s.cta_position,
            margin=0,
            start_seconds=0.0,
            end_seconds=3.0,
            size_preset=getattr(s, "cta_size", None) or "medium",
            overlay_type=overlay_type if overlay_type in ("gif", "mp4") else "png",
            timeout_seconds=60.0,
        )
        video = types.FSInputFile(preview)
        await call.message.answer_video(
            video, caption="Так плашка будет выглядеть на видео (3 сек).",
        )
        await call.answer()
        return
    except Exception as e:
        logger.warning("banner_preview_render_failed", error=str(e)[:200])
        # BUG 7 fix: don't fake MIME — show clear failure message.
        await call.answer("❌ Не удалось сделать предпросмотр. Попробуй другой файл или меньший размер.", show_alert=True)
        return

    photo = types.BufferedInputFile(buf.getvalue(), filename="banner.png")
    await call.message.answer_photo(photo, caption="Так выглядит твоя плашка.")
    await call.answer()


@router.callback_query(F.data == "settings:toggle_cta")
async def on_toggle_cta(call: CallbackQuery) -> None:
    user_id = call.from_user.id if call.from_user else 0
    async with db_manager.session() as session:
        s = await UserSettingsRepository(session).update_fields(user_id)
        s.cta_enabled = not s.cta_enabled
    await on_fine_menu(call)
    await _safe_answer(call, f"Плашка {'включена' if s.cta_enabled else 'выключена'}")


@router.callback_query(F.data == "settings:toggle_subs")
async def on_toggle_subs(call: CallbackQuery) -> None:
    user_id = call.from_user.id if call.from_user else 0
    async with db_manager.session() as session:
        s = await UserSettingsRepository(session).update_fields(user_id)
        s.subtitles_enabled = not s.subtitles_enabled
    await on_fine_menu(call)
    await _safe_answer(call, f"Субтитры {'включены' if s.subtitles_enabled else 'выключены'}")


@router.callback_query(F.data == "settings:position")
async def on_settings_position(call: CallbackQuery) -> None:
    await call.message.edit_text("📍 Положение плашки:", reply_markup=POSITION_MENU)
    await call.answer()


@router.callback_query(F.data == "settings:timing")
async def on_settings_timing(call: CallbackQuery) -> None:
    await call.message.edit_text("⏱ Когда показывать плашку?", reply_markup=TIMING_MENU)
    await call.answer()


CTA_SIZES = {"small": "Маленькая (~28%)", "medium": "Средняя (~33%)", "large": "Большая (~38%)"}


def _resolve_title_text(s) -> str:
    """Title preset id → burned text (empty when none/unknown).

    PHASE D: title_id == "custom" → user-defined text from DB.
    """
    if s is None:
        return ""
    title_id = getattr(s, "title_id", "none") or "none"
    if title_id == "custom":
        return (getattr(s, "custom_title", None) or "").strip()[:100]
    t = TITLES.get(title_id)
    return t.text if t else ""


@router.callback_query(F.data == "appearance:menu")
async def on_appearance_menu(call: CallbackQuery) -> None:
    """🎨 Оформление — summary + presets + плашка (PART 15)."""
    user_id = call.from_user.id if call.from_user else 0
    async with db_manager.session() as session:
        s = await UserSettingsRepository(session).get_or_create(user_id)
    bg = BACKGROUNDS.get(getattr(s, "background_id", "blur"))
    title = TITLES.get(getattr(s, "title_id", "none") or "none")
    style_label = _STYLE_LABELS.get(getattr(s, "style_id", None) or "", "Свой")
    has_banner = bool(s.cta_telegram_file_id)
    text = (
        "🎨 <b>Оформление</b>\n\n"
        f"Стиль: {style_label}\n"
        f"Фон: {bg.label if bg else getattr(s, 'background_id', 'blur')}\n"
        f"Плашка: {'✅' if has_banner else 'нет'}\n"
        f"Заголовок: {title.label if title else 'без текста'}\n"
        "Вставка: нет"
    )
    await call.message.edit_text(
        text, parse_mode="HTML",
        reply_markup=appearance_menu(style_label, has_banner),
    )
    await call.answer()


_STYLE_LABELS = {"clean": "Чистый", "meme": "Мем", "brand": "Бренд", "custom": "Свой"}

def _preset_to_fields(preset_id: str) -> dict:
    """Phase 12 (TZ 12-32-41): ONE registry — BUILTIN_PRESETS in
    services/overlays/presets.py. The handler reads the same objects the
    renderer uses; no separate look/wow dict here."""
    from app.services.overlays.presets import BUILTIN_PRESETS, TransformationPreset
    p: TransformationPreset | None = BUILTIN_PRESETS.get(preset_id)
    if p is None:
        return {}
    return dict(
        background_id=p.background_id,
        title_id=p.title_id,
        brand_corner=p.brand_corner,
        cta_size=getattr(p, "cta_size", None) or "medium",
    )


@router.callback_query(F.data == "style:pick")
async def on_style_pick(call: CallbackQuery) -> None:
    user_id = call.from_user.id if call.from_user else 0
    async with db_manager.session() as session:
        s = await UserSettingsRepository(session).get_or_create(user_id)
    current_id = getattr(s, "style_id", None) or "custom"
    await call.message.edit_text(
        "🎭 Выбери стиль:",
        reply_markup=style_pick_menu(current_id),
    )
    await call.answer()


@router.callback_query(F.data.startswith("style_set:"))
async def on_style_set(call: CallbackQuery) -> None:
    """Apply a style preset to DB (PART 16)."""
    pid = call.data.split(":", 1)[1]
    user_id = call.from_user.id if call.from_user else 0
    if pid not in _STYLE_LABELS:
        await call.answer("Неизвестный стиль")
        return
    async with db_manager.session() as session:
        fields = _preset_to_fields(pid)
        if fields:
            await UserSettingsRepository(session).update_fields(
                user_id, style_id=pid, **fields,
            )
            label = _STYLE_LABELS[pid]
        else:
            # «Свой» — keep current fine settings, just mark as custom.
            await UserSettingsRepository(session).update_fields(user_id, style_id="custom")
            label = "Свой"
    await call.answer(f"Стиль: {label}")
    await on_appearance_menu(call)


@router.callback_query(F.data == "fine:menu")
async def on_fine_menu(call: CallbackQuery) -> None:
    """⚙️ Тонкая настройка — advanced screen (PART 17)."""
    user_id = call.from_user.id if call.from_user else 0
    async with db_manager.session() as session:
        s = await UserSettingsRepository(session).get_or_create(user_id)
    bg = BACKGROUNDS.get(getattr(s, "background_id", "blur"))
    ti = TITLES.get(getattr(s, "title_id", "none") or "none")
    text = (
        "⚙️ <b>Тонкая настройка</b>\n\n"
        f"Фон: {bg.label if bg else '—'}\n"
        f"Заголовок: {ti.label if ti else '—'}\n"
        f"Бренд-уголок: {'ВКЛ' if s.brand_corner else 'ВЫКЛ'}\n"
        f"Плашка: {'ВКЛ' if s.cta_enabled else 'ВЫКЛ'}\n"
        f"Субтитры: {'ВКЛ' if s.subtitles_enabled else 'ВЫКЛ'}"
    )
    await call.message.edit_text(
        text, parse_mode="HTML",
        reply_markup=fine_menu(
            bg.label if bg else "—",
            ti.label if ti else "—",
            bool(getattr(s, "brand_corner", False)),
            bool(s.cta_enabled),
        ),
    )
    await call.answer()


@router.callback_query(F.data == "more:menu")
async def on_more_menu(call: CallbackQuery) -> None:
    """••• Ещё — advanced options for the pending video (PART 14)."""
    user_id = call.from_user.id if call.from_user else 0
    if user_id not in _pending_jobs:
        await call.answer("Сначала пришли видео", show_alert=True)
        return
    await call.message.edit_text(
        "••• <b>Ещё</b>\n\nДополнительные варианты для этого видео:",
        parse_mode="HTML",
        reply_markup=MORE_MENU,
    )
    await call.answer()


@router.callback_query(F.data == "more:back")
async def on_more_back(call: CallbackQuery) -> None:
    user_id = call.from_user.id if call.from_user else 0
    pending = _pending_jobs.get(user_id)
    if pending is not None and Path(pending.input_path).exists():
        await call.message.edit_text(
            "✅ Видео загружено.\n\nВыбери действие:",
            reply_markup=mode_input_menu("prepare"),
        )
    else:
        await call.message.edit_text(HOME_TEXT, parse_mode="HTML", reply_markup=HOME_MENU)
    await call.answer()


@router.callback_query(F.data == "audio:menu")
async def on_audio_menu(call: CallbackQuery) -> None:
    """🔊 Звук — audio presets (PART 22)."""
    user_id = call.from_user.id if call.from_user else 0
    current = "original"
    async with db_manager.session() as session:
        s = await UserSettingsRepository(session).get(user_id)
        if s is not None:
            current = getattr(s, "audio_preset", "original") or "original"
    await call.message.edit_text(
        "🔊 <b>Звук</b>\n\nВыбери режим обработки звука:",
        parse_mode="HTML",
        reply_markup=audio_menu(current),
    )
    await call.answer()


@router.callback_query(F.data.startswith("audio_set:"))
async def on_audio_set(call: CallbackQuery) -> None:
    preset = call.data.split(":", 1)[1]
    if preset not in ("original", "dynamic", "music", "none"):
        await call.answer("Неизвестный режим", show_alert=True)
        return
    user_id = call.from_user.id if call.from_user else 0
    async with db_manager.session() as session:
        repo = UserSettingsRepository(session)
        s = await repo.get_or_create(user_id)
        s.audio_preset = preset
        await session.commit()
    from app.services.media.audio import AUDIO_PRESETS
    label = AUDIO_PRESETS[preset]["label"]
    await call.message.edit_reply_markup(reply_markup=audio_menu(preset))
    await call.answer(f"Звук: {label}")


@router.callback_query(F.data == "style:bg")
async def on_style_bg(call: CallbackQuery) -> None:
    user_id = call.from_user.id if call.from_user else 0
    async with db_manager.session() as session:
        s = await UserSettingsRepository(session).get_or_create(user_id)
    await call.message.edit_text(
        "🎨 Выбери фон:",
        reply_markup=background_menu(getattr(s, "background_id", "blur")),
    )
    await call.answer()


@router.callback_query(F.data == "style:title")
async def on_style_title(call: CallbackQuery) -> None:
    user_id = call.from_user.id if call.from_user else 0
    async with db_manager.session() as session:
        s = await UserSettingsRepository(session).get_or_create(user_id)
    await call.message.edit_text(
        "🏷 Выбери заголовок:",
        reply_markup=title_menu(getattr(s, "title_id", "none")),
    )
    await call.answer()


@router.callback_query(F.data == "style:brand")
async def on_style_brand_toggle(call: CallbackQuery) -> None:
    user_id = call.from_user.id if call.from_user else 0
    async with db_manager.session() as session:
        repo = UserSettingsRepository(session)
        s = await repo.get_or_create(user_id)
        new_val = not bool(s.brand_corner)
        await repo.update_fields(user_id, brand_corner=new_val)
    await call.answer(f"Бренд-уголок {'включён' if new_val else 'выключен'}")
    await on_fine_menu(call)


@router.callback_query(F.data.startswith("style_bg:"))
async def on_set_background(call: CallbackQuery) -> None:
    from app.services.overlays.templates import BACKGROUNDS
    bg_id = call.data.split(":", 1)[1]
    user_id = call.from_user.id if call.from_user else 0
    if bg_id not in BACKGROUNDS:
        await call.answer("Неизвестный фон")
        return
    async with db_manager.session() as session:
        await UserSettingsRepository(session).update_fields(user_id, background_id=bg_id)
    await call.answer(f"Фон: {BACKGROUNDS[bg_id].label}")
    await on_fine_menu(call)


@router.callback_query(F.data.startswith("style_title:"))
async def on_set_title(call: CallbackQuery) -> None:
    title_id = call.data.split(":", 1)[1]
    user_id = call.from_user.id if call.from_user else 0
    if title_id not in TITLES:
        await call.answer("Неизвестный заголовок")
        return
    if title_id == "custom":
        # PHASE D: ask for user text; saved by on_banner_text_while_waiting.
        _awaiting_title.add(user_id)
        await call.message.edit_text(
            "✏️ Пришли свой текст заголовка (до 100 символов).",
            reply_markup=BANNER_CANCEL_MENU,
        )
        await call.answer()
        return
    async with db_manager.session() as session:
        await UserSettingsRepository(session).update_fields(user_id, title_id=title_id)
    await _safe_answer(call, f"Заголовок: {TITLES[title_id].label}")
    await on_fine_menu(call)


@router.callback_query(F.data == "settings:size")
async def on_settings_size(call: CallbackQuery) -> None:
    await call.message.edit_text("📏 Размер плашки:", reply_markup=SIZE_MENU)
    await call.answer()


@router.callback_query(F.data.startswith("cta_size:"))
async def on_set_size(call: CallbackQuery) -> None:
    size = call.data.split(":", 1)[1]
    user_id = call.from_user.id if call.from_user else 0
    if size not in CTA_SIZES:
        await call.answer("Неизвестный размер")
        return
    async with db_manager.session() as session:
        await UserSettingsRepository(session).update_fields(user_id, cta_size=size)
    await call.message.edit_text(
        f"✅ Размер сохранён: {CTA_SIZES[size]}\n\n"
        f"Хочешь посмотреть как будет выглядеть?",
        reply_markup=preview_keyboard(),
    )
    await call.answer()


@router.callback_query(F.data == "settings:upload_cta")
async def on_settings_upload_cta(call: CallbackQuery) -> None:
    # Legacy alias — same flow as banner:upload (PART 20 wording).
    await on_banner_upload_request(call)


_awaiting_banner: set[int] = set()
# PHASE D: users sending a custom title text
_awaiting_title: set[int] = set()


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
    await on_fine_menu(call)
    await _safe_answer(call, "✅ Сохранено")


# ---------------------------------------------------------------------------
# Banner upload handler (user sends PNG)
# ---------------------------------------------------------------------------

async def _handle_banner_asset(message: types.Message, bot: Bot) -> None:
    """Accept a universal overlay asset: PNG / JPEG / WebP / GIF / MP4.

    Called from the video-intake router when the user is in banner-upload
    state (HOTFIX item 5: the intake handler owns ALL documents now).
    Upload state is cleared ONLY after a successful save (item 17).
    """
    user_id = message.from_user.id if message.from_user else 0
    if user_id not in _awaiting_banner:
        return  # not waiting for a banner

    doc = message.document
    if not doc or not doc.file_id:
        await message.answer("❌ Не удалось получить файл.")
        return

    mime = (doc.mime_type or "").lower()
    filename = (doc.file_name or "").lower()
    # (mime prefix, overlay_type, is_animated) — JPEG added (item 18).
    accepted = [
        ("image/png", "png", False),
        ("image/jpeg", "jpeg", False),
        ("image/webp", "webp", False),   # animated webp detected below
        ("image/gif", "gif", True),
        ("video/mp4", "mp4", True),
    ]
    entry = next((e for e in accepted if mime.startswith(e[0])), None)
    # Fallback: trust the filename extension when MIME is octet-stream/None.
    if entry is None:
        ext_map = {".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg",
                   ".webp": "webp", ".gif": "gif", ".mp4": "mp4"}
        for ext, otype in ext_map.items():
            if filename.endswith(ext):
                entry = ("", otype, otype in ("gif", "mp4"))
                break
    if entry is None:
        await message.answer(
            "❌ Неподдерживаемый формат: " + (mime or "неизвестен") + ".\n\n"
            "Поддерживаются: PNG, JPEG, WebP, GIF или короткий MP4."
        )
        return  # waiting state KEPT — the next valid file must be accepted
    overlay_type, is_animated = entry[1], entry[2]

    # Download temporarily, validate, then store file_id in DB.
    try:
        _USER_ASSETS_DIR.mkdir(parents=True, exist_ok=True)
        ext = {"png": "png", "jpeg": "jpg", "webp": "webp", "gif": "gif", "mp4": "mp4"}[overlay_type]
        tmp_path = _USER_ASSETS_DIR / f"_tmp_banner_{user_id}.{ext}"
        file = await bot.get_file(doc.file_id)
        await bot.download_file(file.file_path, destination=tmp_path)
    except Exception as e:
        logger.error("banner_download_failed", user_id=user_id, error=str(e)[:200])
        await message.answer("❌ Не удалось скачать файл.")
        return

    # Content detection via Pillow (item 18) — MIME alone is not trusted.
    if overlay_type in ("png", "jpeg", "webp"):
        try:
            from PIL import Image
            img = Image.open(tmp_path)
            fmt = (img.format or "").upper()
            fmt_to_type = {"PNG": "png", "JPEG": "jpeg", "WEBP": "webp"}
            if fmt_to_type.get(fmt) != overlay_type:
                tmp_path.unlink(missing_ok=True)
                await message.answer(
                    f"❌ Внутри файла не {overlay_type.upper()} "
                    f"(определён формат: {fmt or 'неизвестен'})."
                )
                return
            if overlay_type == "webp":
                # Animated WebP: n_frames > 1
                is_animated = getattr(img, "n_frames", 1) > 1
            if img.mode not in ("RGBA", "RGB"):
                img = img.convert("RGBA")
            w, h = img.size
            if w <= 0 or h <= 0:
                tmp_path.unlink(missing_ok=True)
                await message.answer("❌ Изображение повреждено.")
                return
        except Exception:
            tmp_path.unlink(missing_ok=True)
            await message.answer("❌ Не удалось прочитать изображение.")
            return
    else:
        # GIF / MP4: validate via probe (ffmpeg must read it).
        try:
            from app.services.media.probe import get_probe_service
            m = await get_probe_service().probe(tmp_path)
            if m.duration_seconds <= 0:
                raise ValueError("no duration")
        except Exception:
            tmp_path.unlink(missing_ok=True)
            await message.answer("❌ Не удалось прочитать файл. Попробуй другой формат.")
            return

    # Store Telegram file_id + overlay meta in DB (survives Railway redeploy).
    async with db_manager.session() as session:
        await UserSettingsRepository(session).update_fields(
            user_id,
            cta_telegram_file_id=doc.file_id,
            overlay_type=overlay_type,
            overlay_is_animated=is_animated,
            cta_enabled=True,
        )
    tmp_path.unlink(missing_ok=True)
    # State cleared ONLY after a successful save (item 17).
    _awaiting_banner.discard(user_id)

    kind = "статичная" if not is_animated else "анимированная"
    await message.answer(f"✅ Плашка сохранена ({kind})")
    from app.bot.keyboards.inline import banner_menu
    await message.answer("Настройки плашки:", reply_markup=banner_menu(True))


# ---------------------------------------------------------------------------
# Banner wrong-input response (explicit state when user taps "Upload banner")
# ---------------------------------------------------------------------------

@router.message(F.photo)
async def on_banner_photo_wrong_input(message: types.Message) -> None:
    """User sent a photo (not a file). Must respond clearly when in banner-upload state."""
    user_id = message.from_user.id if message.from_user else 0
    if user_id in _awaiting_banner:
        await message.answer(
            "❌ Ты отправил изображение как фото.\n\n"
            "Пришли PNG через:\n"
            "Скрепка → Файл\n\n"
            "Это нужно, чтобы сохранить качество и прозрачность.",
            reply_markup=BANNER_CANCEL_MENU,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pick_attachment(message: types.Message):
    """Return the biggest video attachment, or None (HOTFIX item 5).

    MIME is a hint only: documents with octet-stream/None MIME are returned
    and validated by ffprobe after download. Clearly non-video documents
    (explicit non-video MIME + non-video extension) are skipped here so a
    stray PDF doesn't trigger a download.
    """
    candidates = []
    if message.video:
        candidates.append(message.video)
    if message.video_note:
        candidates.append(message.video_note)
    if message.animation:
        candidates.append(message.animation)
    doc = message.document
    if doc is not None:
        mime = (doc.mime_type or "").lower()
        filename = (doc.file_name or "").lower()
        video_exts = (".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v",
                      ".mpg", ".mpeg", ".ts", ".wmv", ".flv", ".3gp")
        clearly_video = (
            mime.startswith("video/")
            or mime in ("", "application/octet-stream")
            or mime.startswith("application/x-matroska")
            or filename.endswith(video_exts)
        )
        if clearly_video:
            candidates.append(doc)
    if not candidates:
        return None
    return max(candidates, key=lambda a: getattr(a, "file_size", 0) or 0)


async def _safe_answer(call: CallbackQuery, text: str | None = None,
                       show_alert: bool = False) -> None:
    """Answer a callback, ignoring expired-query errors."""
    try:
        await call.answer(text, show_alert=show_alert)
    except Exception as e:
        logger.warning("callback_answer_failed", error=str(e)[:120])


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


async def _edit_status(message: types.Message, text: str, reply_markup=None) -> None:
    """Edit a status message. We swallow Bad Request in case the original
    message was deleted or is too old for edits.
    """
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except Exception as e:
        logger.warning("status_edit_failed", error=str(e)[:120])