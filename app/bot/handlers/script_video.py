"""Telegram flow for creating a short video from a text idea."""
from __future__ import annotations

import asyncio
import gc
import os
import tempfile
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiogram import F, Router
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter, TelegramServerError
from aiogram.types import CallbackQuery, FSInputFile, Message
from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.logging import get_logger
from app.database.models import Job, JobStatus
from app.database.repositories import JobRepository, UserSettingsRepository
from app.database.session import db_manager
from app.services.script_video import ScriptPlanner, ScriptVideoAssembler, get_cost_tracker
from app.services.script_video.hybrid_media import HybridMediaResolver, HybridMediaResult
from app.services.script_video.image_generator import ImageCreditsExhaustedError
from app.services.script_video.tts_adapter import ScriptTTSAdapter
from app.services.subtitles.scene_adapter import SceneSubtitleBuilder

router = Router()
logger = get_logger(__name__)
_awaiting_idea: set[int] = set()
_script_requests: set[int] = set()
_script_tasks: dict[int, asyncio.Task] = {}
_script_render_semaphore = asyncio.Semaphore(1)

QUALITY_IMAGE_MODEL = "recraft/recraft-v4.1-flash"
QUALITY_TTS_MODEL = "elevenlabs/eleven-v4-turbo"
QUALITY_TTS_VOICE = "george"
QUALITY_AI_KEYFRAMES = 4
SUBTITLE_DELAY_SECONDS = 0.12
MAX_SCRIPT_QUEUE = 8
DEFAULT_COST_GUARD_USD = 0.05
DEFAULT_IMAGE_UNIT_COST_USD = 0.007
DEFAULT_TTS_RESERVE_USD = 0.015
DEFAULT_STOCK_MAX_MB = 25


@router.callback_query(F.data == "script_video:start")
async def start_script_video(call: CallbackQuery) -> None:
    user_id = call.from_user.id
    _awaiting_idea.add(user_id)
    await call.answer()
    if call.message:
        await call.message.answer(
            "📝 Напиши тему ролика одним сообщением. Например: «Почему мы откладываем важные дела».",
            reply_markup=None,
        )


def reset_state(user_id: int) -> None:
    _awaiting_idea.discard(user_id)


def cancel_script_video(user_id: int) -> bool:
    """Cancel a running/waiting Script Video coroutine for this user."""
    _awaiting_idea.discard(user_id)
    task = _script_tasks.get(user_id)
    if task is None or task.done():
        return False
    current = asyncio.current_task()
    if task is current:
        return False
    task.cancel()
    return True


def _env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _cost_capped_ai_budget(current_actual_cost: float) -> tuple[int, dict[str, float]]:
    configured = _env_int("SCRIPT_VIDEO_MAX_AI_IMAGES", QUALITY_AI_KEYFRAMES, 0, 8)
    guard = _env_float("SCRIPT_VIDEO_COST_GUARD_USD", DEFAULT_COST_GUARD_USD, 0.01, 1.0)
    image_unit = _env_float(
        "SCRIPT_VIDEO_IMAGE_UNIT_COST_USD", DEFAULT_IMAGE_UNIT_COST_USD, 0.001, 0.20
    )
    tts_reserve = _env_float(
        "SCRIPT_VIDEO_TTS_RESERVE_USD", DEFAULT_TTS_RESERVE_USD, 0.0, 0.20
    )
    available = max(0.0, guard - current_actual_cost - tts_reserve)
    affordable = max(0, int((available + 1e-9) // image_unit))
    budget = min(configured, affordable)
    return budget, {
        "guard": guard,
        "image_unit": image_unit,
        "tts_reserve": tts_reserve,
        "available_for_images": available,
    }


async def _build_synced_subtitles(plan, audio_path: Path, audio_duration: float):
    """Use free local word timestamps when possible, with safe plan fallback.

    The lightweight Whisper model is deliberately job-scoped. ReCut also has a
    separate long-video Whisper service, so keeping both models resident on a
    small Railway container can trigger OOM later. Releasing the alignment model
    after every Script Video trades a little latency for predictable memory.
    """
    builder = SceneSubtitleBuilder()
    fallback_delay = _env_float(
        "SCRIPT_VIDEO_SUBTITLE_DELAY_SECONDS", SUBTITLE_DELAY_SECONDS, 0.0, 0.5
    )
    if _env_bool("SCRIPT_VIDEO_WORD_ALIGNMENT_ENABLED", True):
        alignment_service = None
        try:
            from app.services.transcription.base import TranscriptionRequest
            from app.services.transcription.faster_whisper import WhisperTranscriptionService

            alignment_model = os.getenv("SCRIPT_VIDEO_ALIGNMENT_MODEL", "tiny").strip() or "tiny"
            settings = get_settings()
            alignment_service = WhisperTranscriptionService(
                model=alignment_model,
                device=settings.whisper_device,
                compute_type=settings.whisper_compute_type,
            )
            result = await alignment_service.transcribe(
                TranscriptionRequest(
                    audio_path=str(audio_path),
                    language=plan.language,
                    beam_size=1,
                    word_timestamps=True,
                )
            )
            words = tuple(word for segment in result.segments for word in segment.words)
            expected_words = len((plan.narration or "").split())
            if len(words) >= max(3, int(expected_words * 0.55)):
                subtitles = builder.build_from_words(words)
                logger.info(
                    "script_subtitles_word_aligned",
                    words=len(words),
                    model=result.model,
                    duration_seconds=result.duration_seconds,
                )
                return subtitles
            logger.warning(
                "script_subtitle_alignment_too_sparse",
                aligned_words=len(words),
                expected_words=expected_words,
            )
        except Exception as exc:
            logger.warning(
                "script_subtitle_alignment_fallback",
                error_type=type(exc).__name__,
            )
        finally:
            alignment_service = None
            gc.collect()

    return builder.build_from_plan(
        plan,
        start_offset=fallback_delay,
        total_duration=max(0.5, audio_duration - fallback_delay),
    )


async def _send_result_video_with_retry(
    message: Message,
    output: Path,
    caption: str,
) -> None:
    """Retry only transient Telegram delivery failures, never rerun AI work."""
    for attempt in range(3):
        try:
            await message.answer_video(
                video=FSInputFile(str(output), filename="recut-video.mp4"),
                caption=caption,
                supports_streaming=True,
                parse_mode=None,
            )
            return
        except TelegramRetryAfter as exc:
            if attempt == 2:
                raise
            await asyncio.sleep(min(10.0, max(1.0, float(exc.retry_after))))
        except (TelegramNetworkError, TelegramServerError):
            if attempt == 2:
                raise
            await asyncio.sleep(1.5 * (attempt + 1))


async def handle_idea(message: Message, bot=None) -> bool:
    """Run generation for a user who tapped the create-video button."""
    user_id = message.from_user.id if message.from_user else 0
    if not user_id or user_id not in _awaiting_idea:
        return False
    idea = (message.text or "").strip()
    if len(idea) < 8:
        await message.answer("Добавь немного деталей к теме (не меньше 8 символов).")
        return True
    _awaiting_idea.discard(user_id)
    if user_id in _script_requests:
        await message.answer("⏳ Уже создаю для тебя ролик. Подожди завершения.")
        return True
    if len(_script_requests) >= MAX_SCRIPT_QUEUE:
        await message.answer("⏳ Очередь создания роликов заполнена. Попробуй чуть позже.")
        return True

    settings = get_settings()
    if not settings.openrouter_api_key:
        await message.answer("Создание роликов временно недоступно. Попробуй позже.")
        return True

    if bot is not None:
        from app.bot.handlers.video import _subscription_gate_blocked
        if await _subscription_gate_blocked(bot, user_id, message.answer):
            return True

    is_premium = False
    is_unlimited_tester = (
        settings.has_unlimited_generations(user_id)
        or user_id in settings.internal_user_ids
    )
    async with db_manager.session() as session:
        user_settings = await UserSettingsRepository(session).get(user_id)
        if user_settings:
            from app.services.appearance import is_premium as has_premium
            is_premium = has_premium(user_settings)
        if settings.script_video_enable_limit and not is_premium and not is_unlimited_tester:
            cutoff = datetime.now(timezone.utc) - timedelta(days=7)
            completed = await session.scalar(
                select(func.count(Job.id)).where(
                    Job.telegram_user_id == user_id,
                    Job.source_filename == "script-video",
                    Job.status == JobStatus.COMPLETED,
                    Job.created_at >= cutoff,
                )
            ) or 0
            if completed >= settings.script_video_free_weekly_limit:
                await message.answer(
                    "На этой неделе уже использованы бесплатные ролики. Попробуй позже."
                )
                return True

    _script_requests.add(user_id)
    current_task = asyncio.current_task()
    if current_task is not None:
        _script_tasks[user_id] = current_task

    job_id: int | None = None
    subtitle_path: Path | None = None
    media: HybridMediaResult | None = None
    status = await message.answer("🧠 Готовлю сценарий и создаю ролик… Это может занять несколько минут.")
    tracker = get_cost_tracker(
        user_id,
        f"script_{user_id}_{message.message_id}",
        image_model=QUALITY_IMAGE_MODEL,
        tts_model=QUALITY_TTS_MODEL,
    )
    try:
        async with db_manager.session() as session:
            repo = JobRepository(session)
            if await repo.has_active_job(user_id):
                await status.edit_text("⏳ У тебя уже выполняется другая задача. Дождись её завершения.")
                return True
            job = await repo.create(
                telegram_user_id=user_id,
                telegram_chat_id=message.chat.id,
                source_message_id=message.message_id,
                source_filename="script-video",
            )
            job_id = job.id

        if _script_render_semaphore.locked():
            with suppress(Exception):
                await status.edit_text("⏳ Ролик в очереди. Начну, как освободится рендер.")

        async with _script_render_semaphore:
            if job_id is not None:
                async with db_manager.session() as session:
                    await JobRepository(session).set_status(job_id, JobStatus.ANALYZING)

            with tempfile.TemporaryDirectory(prefix=f"recut-script-{user_id}-") as temp:
                workdir = Path(temp)
                planner = ScriptPlanner()
                try:
                    plan = await planner.plan(
                        user_idea=idea,
                        target_duration=settings.script_video_target_duration_seconds,
                        language="ru",
                    )
                finally:
                    tracker.add_actual_cost("planner", planner.actual_cost_usd)

                ai_budget, budget_meta = _cost_capped_ai_budget(tracker.actual_cost_usd)
                logger.info(
                    "script_video_cost_preflight",
                    user_id=user_id,
                    job_id=job_id,
                    planner_cost_usd=tracker.actual_cost_usd,
                    ai_budget=ai_budget,
                    **budget_meta,
                )
                max_stock_mb = _env_int(
                    "SCRIPT_VIDEO_MAX_STOCK_MB", DEFAULT_STOCK_MAX_MB, 8, 80
                )
                media = await HybridMediaResolver(
                    image_model=QUALITY_IMAGE_MODEL,
                    fallback_image_model="",
                    cost_tracker=tracker,
                    max_ai_images=ai_budget,
                    max_stock_download_mb=max_stock_mb,
                ).resolve(plan, workdir)
                tracker.images_generated = media.ai_images

                audio_path = workdir / "narration.mp3"
                narration = plan.narration or " ".join(scene.narration for scene in plan.scenes)
                tts = ScriptTTSAdapter(
                    model=QUALITY_TTS_MODEL,
                    voice=QUALITY_TTS_VOICE,
                    fallback_model="",
                )
                try:
                    await tts.synthesize_narration(narration, audio_path)
                finally:
                    tracker.add_actual_cost("tts", tts.actual_cost_usd)
                audio_duration = await ScriptVideoAssembler.audio_duration(audio_path)
                tracker.audio_duration_seconds = audio_duration

                subtitles = await _build_synced_subtitles(plan, audio_path, audio_duration)
                subtitle_path = Path(subtitles.output_path) if subtitles.output_path else None
                output = workdir / "recut-video.mp4"
                if job_id is not None:
                    async with db_manager.session() as session:
                        await JobRepository(session).set_status(job_id, JobStatus.RENDERING)
                await ScriptVideoAssembler().assemble(
                    plan=plan,
                    images=[str(path) for path in media.paths],
                    subtitle_path=str(subtitle_path) if subtitle_path else None,
                    audio_path=audio_path,
                    output_path=output,
                )
                if not output.is_file() or output.stat().st_size == 0:
                    raise RuntimeError("video output is empty")

                caption = f"✅ {plan.title}\nГотово — вертикальный ролик с озвучкой и субтитрами."
                if media.attribution_text:
                    caption += f"\n\nB-roll: {media.attribution_text}"
                await _send_result_video_with_retry(message, output, caption)

        if job_id is not None:
            async with db_manager.session() as session:
                await JobRepository(session).mark_completed(job_id, clips_generated=1)
        logger.info(
            "script_video_completed",
            user_id=user_id,
            job_id=job_id,
            ai_images=media.ai_images if media else 0,
            stock_videos=media.stock_videos if media else 0,
            reused_scenes=media.reused_scenes if media else 0,
            actual_cost_usd=tracker.actual_cost_usd,
            actual_cost_breakdown=tracker.actual_costs,
            # The TTS API may not expose exact charges. Do not present that
            # missing value as zero: show a separately labeled estimate.
            tts_cost_confirmed=tts.cost_confirmed,
            tts_cost_estimate_usd=round(tts.estimated_cost_usd, 6),
            total_cost_estimate_usd=round(
                tracker.actual_cost_usd
                + (0.0 if tts.cost_confirmed else tts.estimated_cost_usd),
                6,
            ),
        )
        with suppress(Exception):
            await status.delete()
        if bot is not None:
            from app.bot.handlers.video import _offer_subscription_after_first_result
            with suppress(Exception):
                await _offer_subscription_after_first_result(bot, user_id, message)
    except asyncio.CancelledError:
        logger.info(
            "script_video_cancelled",
            user_id=user_id,
            job_id=job_id,
            actual_cost_usd=tracker.actual_cost_usd,
            actual_cost_breakdown=tracker.actual_costs,
        )
        if job_id is not None:
            with suppress(Exception):
                async with db_manager.session() as session:
                    await JobRepository(session).set_status(job_id, JobStatus.CANCELLED)
        with suppress(Exception):
            await status.edit_text("⛔️ Создание ролика отменено.")
        return True
    except Exception as exc:
        logger.exception(
            "script_video_failed",
            user_id=user_id,
            job_id=job_id,
            error_type=type(exc).__name__,
            actual_cost_usd=tracker.actual_cost_usd,
            actual_cost_breakdown=tracker.actual_costs,
        )
        if job_id is not None:
            try:
                async with db_manager.session() as session:
                    await JobRepository(session).mark_failed(
                        job_id, "SCRIPT_VIDEO_FAILED", type(exc).__name__
                    )
            except Exception:
                logger.exception("script_video_job_failure_record_failed", job_id=job_id)
        if isinstance(exc, ImageCreditsExhaustedError):
            with suppress(Exception):
                await status.edit_text(
                    "❌ Не удалось создать ролик: закончился баланс OpenRouter для генерации изображений. "
                    "Попробуй позже или сообщи администратору бота."
                )
        else:
            with suppress(Exception):
                await status.edit_text("❌ Не удалось создать ролик. Попробуй ещё раз чуть позже.")
    finally:
        if subtitle_path:
            subtitle_path.unlink(missing_ok=True)
        _script_requests.discard(user_id)
        if _script_tasks.get(user_id) is current_task:
            _script_tasks.pop(user_id, None)
    return True


@router.message()
async def on_script_video_request(message: Message) -> None:
    await handle_idea(message)
