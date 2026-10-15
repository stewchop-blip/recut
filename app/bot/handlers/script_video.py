"""Telegram flow for creating a short video from a text idea."""
from __future__ import annotations

import base64
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from aiogram import F, Router
from aiogram.types import CallbackQuery, FSInputFile, Message
from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.logging import get_logger
from app.database.models import Job, JobStatus
from app.database.repositories import JobRepository, UserSettingsRepository
from app.database.session import db_manager
from app.services.script_video import ImageGenerator, ScriptPlanner, ScriptVideoAssembler, get_cost_tracker
from app.services.script_video.image_generator import ImageCreditsExhaustedError
from app.services.script_video.tts_adapter import ScriptTTSAdapter
from app.services.subtitles.scene_adapter import SceneSubtitleBuilder

router = Router()
logger = get_logger(__name__)
_awaiting_idea: set[int] = set()
_script_requests: set[int] = set()


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

    # Reuse the existing first-result subscription gate for this output too.
    if bot is not None:
        from app.bot.handlers.video import _subscription_gate_blocked
        if await _subscription_gate_blocked(bot, user_id, message.answer):
            return True

    is_premium = False
    async with db_manager.session() as session:
        user_settings = await UserSettingsRepository(session).get(user_id)
        if user_settings:
            from app.services.appearance import is_premium as has_premium
            is_premium = has_premium(user_settings)
        if settings.script_video_enable_limit and not is_premium:
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

            prompts = [scene.visual_prompt for scene in plan.scenes]
            image_cap = settings.script_video_max_images_pro if is_premium else settings.script_video_max_images_free
            image_results = await ImageGenerator(cost_tracker=tracker).generate(
                prompts, size="1024x1024", max_images=image_cap,
            )
            if not image_results:
                raise RuntimeError("image generation returned no images")
            image_paths: list[Path] = []
            async with httpx.AsyncClient(timeout=45, follow_redirects=True) as client:
                for index, item in enumerate(image_results):
                    if item.startswith("data:"):
                        header, encoded = item.split(",", 1)
                        image_bytes = base64.b64decode(encoded, validate=True)
                        ext = ".jpg" if "jpeg" in header else ".png"
                    else:
                        response = await client.get(item)
                        response.raise_for_status()
                        image_bytes = response.content
                        content_type = response.headers.get("content-type", "")
                        ext = ".jpg" if "jpeg" in content_type else ".png"
                    if len(image_bytes) > 15 * 1024 * 1024:
                        raise RuntimeError("generated image exceeds size limit")
                    path = workdir / f"scene-{index:02d}{ext}"
                    path.write_bytes(image_bytes)
                    image_paths.append(path)

            tracker.images_generated = len(image_paths)
            audio_path = workdir / "narration.mp3"
            narration = plan.narration or " ".join(scene.narration for scene in plan.scenes)
            await ScriptTTSAdapter().synthesize_narration(narration, audio_path)
            audio_duration = await ScriptVideoAssembler.audio_duration(audio_path)
            tracker.audio_duration_seconds = audio_duration
            subtitles = SceneSubtitleBuilder().build_from_plan(
                plan, total_duration=audio_duration,
            )
            subtitle_path = Path(subtitles.output_path) if subtitles.output_path else None
            output = workdir / "recut-video.mp4"
            await ScriptVideoAssembler().assemble(
                plan=plan,
                images=[str(path) for path in image_paths],
                subtitle_path=str(subtitle_path) if subtitle_path else None,
                audio_path=audio_path,
                output_path=output,
            )
            if not output.is_file() or output.stat().st_size == 0:
                raise RuntimeError("video output is empty")
            await message.answer_video(
                video=FSInputFile(str(output), filename="recut-video.mp4"),
                caption=f"✅ {plan.title}\nГотово — вертикальный ролик с озвучкой и субтитрами.",
                supports_streaming=True,
            )

        async with db_manager.session() as session:
            await JobRepository(session).mark_completed(job_id, clips_generated=1)
        cost = tracker.actual_cost_usd
        logger.info("script_video_completed", user_id=user_id, job_id=job_id,
                    images=tracker.images_generated, actual_cost_usd=cost,
                    actual_cost_breakdown=tracker.actual_costs)
        await status.delete()
        if bot is not None:
            from app.bot.handlers.video import _offer_subscription_after_first_result
            await _offer_subscription_after_first_result(bot, user_id, message)
    except Exception as exc:
        logger.exception("script_video_failed", user_id=user_id, job_id=job_id,
                         error_type=type(exc).__name__)
        if job_id is not None:
            try:
                async with db_manager.session() as session:
                    await JobRepository(session).mark_failed(job_id, "SCRIPT_VIDEO_FAILED", type(exc).__name__)
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
    # The regular URL handler has priority; this remains a fallback for routers
    # mounted without that handler.
    await handle_idea(message)
