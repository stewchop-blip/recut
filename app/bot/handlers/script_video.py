"""Telegram flow for creating a short video from a text idea."""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiogram import F, Router
from aiogram.types import CallbackQuery, FSInputFile, Message
from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.logging import get_logger
from app.database.models import Job, JobStatus
from app.database.repositories import JobRepository, UserSettingsRepository
from app.database.session import db_manager
from app.services.script_video import ScriptPlanner, ScriptVideoAssembler, get_cost_tracker
from app.services.script_video.hybrid_media import HybridMediaResolver
from app.services.script_video.image_generator import ImageCreditsExhaustedError
from app.services.script_video.tts_adapter import ScriptTTSAdapter
from app.services.subtitles.scene_adapter import SceneSubtitleBuilder

router = Router()
logger = get_logger(__name__)
_awaiting_idea: set[int] = set()
_script_requests: set[int] = set()

# Quality V3: real B-roll for generic scenes + a small paid AI keyframe budget.
# With no stock API key configured, HybridMediaResolver intentionally keeps the
# known-good all-AI V2 path, so this change is safe to roll back or deploy early.
QUALITY_IMAGE_MODEL = "recraft/recraft-v4.1-flash"
QUALITY_TTS_MODEL = "elevenlabs/eleven-v4-turbo"
QUALITY_TTS_VOICE = "george"
QUALITY_AI_KEYFRAMES = 4
SUBTITLE_DELAY_SECONDS = 0.12


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

    settings = get_settings()
    if not settings.openrouter_api_key:
        await message.answer("Создание роликов временно недоступно. Попробуй позже.")
        return True

    if bot is not None:
        from app.bot.handlers.video import _subscription_gate_blocked
        if await _subscription_gate_blocked(bot, user_id, message.answer):
            return True

    is_premium = False
    is_unlimited_tester = settings.has_unlimited_generations(user_id)
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
    job_id: int | None = None
    subtitle_path: Path | None = None
    status = await message.answer("🧠 Готовлю сценарий и создаю ролик… Это может занять несколько минут.")
    tracker = get_cost_tracker(user_id, f"script_{user_id}_{message.message_id}")
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

        with tempfile.TemporaryDirectory(prefix=f"recut-script-{user_id}-") as temp:
            workdir = Path(temp)
            planner = ScriptPlanner()
            plan = await planner.plan(
                user_idea=idea,
                target_duration=settings.script_video_target_duration_seconds,
                language="ru",
            )
            tracker.add_actual_cost("planner", planner.actual_cost_usd)

            ai_budget = max(
                1,
                min(
                    8,
                    int(os.getenv("SCRIPT_VIDEO_MAX_AI_IMAGES", str(QUALITY_AI_KEYFRAMES))),
                ),
            )
            media = await HybridMediaResolver(
                image_model=QUALITY_IMAGE_MODEL,
                fallback_image_model=settings.script_video_image_model,
                cost_tracker=tracker,
                max_ai_images=ai_budget,
            ).resolve(plan, workdir)
            tracker.images_generated = media.ai_images

            audio_path = workdir / "narration.mp3"
            narration = plan.narration or " ".join(scene.narration for scene in plan.scenes)
            await ScriptTTSAdapter(
                model=QUALITY_TTS_MODEL,
                voice=QUALITY_TTS_VOICE,
            ).synthesize_narration(narration, audio_path)
            audio_duration = await ScriptVideoAssembler.audio_duration(audio_path)
            tracker.audio_duration_seconds = audio_duration

            subtitle_delay = max(
                0.0,
                min(
                    0.5,
                    float(os.getenv("SCRIPT_VIDEO_SUBTITLE_DELAY_SECONDS", str(SUBTITLE_DELAY_SECONDS))),
                ),
            )
            subtitles = SceneSubtitleBuilder().build_from_plan(
                plan,
                start_offset=subtitle_delay,
                total_duration=max(0.5, audio_duration - subtitle_delay),
            )
            subtitle_path = Path(subtitles.output_path) if subtitles.output_path else None
            output = workdir / "recut-video.mp4"
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
            await message.answer_video(
                video=FSInputFile(str(output), filename="recut-video.mp4"),
                caption=caption,
                supports_streaming=True,
            )

        async with db_manager.session() as session:
            await JobRepository(session).mark_completed(job_id, clips_generated=1)
        cost = tracker.actual_cost_usd
        logger.info(
            "script_video_completed",
            user_id=user_id,
            job_id=job_id,
            ai_images=media.ai_images,
            stock_videos=media.stock_videos,
            reused_scenes=media.reused_scenes,
            actual_cost_usd=cost,
            actual_cost_breakdown=tracker.actual_costs,
        )
        await status.delete()
        if bot is not None:
            from app.bot.handlers.video import _offer_subscription_after_first_result
            await _offer_subscription_after_first_result(bot, user_id, message)
    except Exception as exc:
        logger.exception(
            "script_video_failed",
            user_id=user_id,
            job_id=job_id,
            error_type=type(exc).__name__,
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
            await status.edit_text(
                "❌ Не удалось создать ролик: закончился баланс OpenRouter для генерации изображений. "
                "Попробуй позже или сообщи администратору бота."
            )
        else:
            await status.edit_text("❌ Не удалось создать ролик. Попробуй ещё раз чуть позже.")
    finally:
        if subtitle_path:
            subtitle_path.unlink(missing_ok=True)
        _script_requests.discard(user_id)
    return True


@router.message()
async def on_script_video_request(message: Message) -> None:
    await handle_idea(message)
