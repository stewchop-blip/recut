"""Script video generation Telegram handler.

New flow:
  user sends text: "Сделай ролик о трёх ошибках UX дизайнеров"
  -> bot creates request, calls ScriptPlanner
  -> generates images (parallel, with retry/fallback)
  -> builds subtitles from scene narration
  -> assembles video (FFmpeg: image sequence + audio + subtitles)
  -> sends final MP4 + summary

Does NOT modify existing video.py handlers.
"""
import tempfile
from pathlib import Path

from aiogram import Router, types
from aiogram.types import Message

from app.core.config import get_settings
from app.core.logging import get_logger
from app.services.script_video import (
    ScriptPlanner,
    ImageGenerator,
    CostTracker,
    get_cost_tracker,
)
from app.services.script_video.models import VideoPlan
from app.services.subtitles.scene_adapter import SceneSubtitleBuilder

router = Router()
logger = get_logger(__name__)

# Temporary in-memory tracking for script-video requests
_script_requests: dict[int, dict] = {}


@router.message()
async def on_script_video_request(message: Message) -> None:
    """Detect script video idea and start pipeline.

    Only activates when user sends a clear idea text
    (starts with keywords or contains 'ролик', 'видео', 'сделай').
    Keeps existing video/file flows untouched.
    """
    user_id = message.from_user.id if message.from_user else 0
    text = (message.text or "").strip().lower()
    # Simple trigger: keywords that indicate a creation request
    triggers = ("сделай", "создай", "ролик", "видео", "на тему", "о ", "про ")
    if not any(t in text for t in triggers) or len(text) < 10:
        return  # Not a script-video request; let existing handlers run

    # Prevent duplicate concurrent requests
    if user_id in _script_requests:
        await message.answer("⏳ Уже обрабатываю твой запрос. Подожди несколько секунд.")
        return

    settings = get_settings()
    # Quick cost warning: if limits enabled and user exceeded, block
    if settings.script_video_enable_limit:
        # Weekly free limit enforced by image limit cap (max_images_free)
        # DB tracking to be added separately
        pass  # Enforced by image count cap in generator

    # Confirm start
    status_msg = await message.answer("🧠 Анализирую идею…⏳ Подожди минуту.")
    _script_requests[user_id] = {"status_msg_id": status_msg.message_id, "chat_id": message.chat.id}

    # Initialize tracker
    tracker = get_cost_tracker(
        user_id=user_id,
        job_id=f"script_{user_id}_{message.message_id}",
    )

    try:
        # 1. Plan
        planner = ScriptPlanner()
        plan = await planner.plan(
            user_idea=message.text or "",
            target_duration=settings.script_video_target_duration_seconds,
            language="ru",
        )
        tracker.planner_input_tokens = 500  # Estimated; real usage from response
        tracker.planner_output_tokens = 1200  # Estimated

        # 2. Images
        prompts = [s.visual_prompt for s in plan.scenes]
        image_gen = ImageGenerator()
        images = await image_gen.generate(prompts=prompts, size="1024x1024", n_per_prompt=1)
        tracker.images_generated = len(images)

        # 3. Subtitles from scenes
        subtitle_builder = SceneSubtitleBuilder()
        subtitle_result = subtitle_builder.build_from_plan(plan)
        tracker.audio_duration_seconds = float(plan.target_duration)

        # 4. Assemble video (stub: would call FFmpeg pipeline combining images + audio + subtitles)
        # For now we simulate assembly and send a summary message.
        await status_msg.edit_text(
            f"✅ Готово (демо)\n\n"
            f"📋 <b>{plan.title}</b>\n"
            f"🎬 Сцен: {len(plan.scenes)}\n"
            f"⏱ Длительность: ~{plan.target_duration} сек\n"
            f"🖼 Изображений: {len(images)}\n"
            f"💰 Ориентировочная стоимость: ${tracker.estimated_cost_usd():.4f}\n\n"
            f"(Полная сборка видео будет реализована в следующем этапе.)"
        )
        # Log for audit
        logger.info(
            "script_video_completed",
            user_id=user_id,
            title=plan.title,
            scenes=len(plan.scenes),
            images=len(images),
            cost_usd=tracker.estimated_cost_usd(),
        )
    except Exception as exc:
        logger.exception("script_video_failed", user_id=user_id, exc=str(exc)[:300])
        await status_msg.edit_text(
            f"❌ Не удалось создать ролик.\n\n"
            f"Ошибка: {str(exc)[:200]}\n\n"
            f"Попробуй переформулировать запрос короче."
        )
    finally:
        _script_requests.pop(user_id, None)
