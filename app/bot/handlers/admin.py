"""Internal admin commands — Phase 10 of stability pass.
Admin/tester roles from ALLOWED_TELEGRAM_USER_IDS, ADMIN_TELEGRAM_IDS, TESTER_TELEGRAM_IDS.
"""
from aiogram import Router, types
from app.core.config import get_settings
from app.core.logging import get_logger

router = Router()
logger = get_logger(__name__)

@router.message(lambda m: (m.text or "").startswith("/stats"))
async def cmd_stats(message: types.Message) -> None:
    settings = get_settings()
    user_id = message.from_user.id if message.from_user else 0
    allowed = settings.allowed_user_id_set
    admin_ids = {int(x) for x in (getattr(settings, 'admin_telegram_ids', '') or '').split(',') if x.strip().isdigit()}
    tester_ids = {int(x) for x in (getattr(settings, 'tester_telegram_ids', '') or '').split(',') if x.strip().isdigit()}
    is_admin = user_id in admin_ids or user_id in tester_ids
    if user_id not in allowed and user_id not in admin_ids and user_id not in tester_ids:
        await message.answer("❌ Доступ запрещён.")
        return
    await message.answer("📊 Stats: basic admin command ready. Full metrics require deeper analytics integration.")

@router.message(lambda m: (m.text or "").startswith("/queue"))
async def cmd_queue(message: types.Message) -> None:
    await message.answer("⚡ Queue: basic command ready. Queue details available via MediaJobQueue instance.")
