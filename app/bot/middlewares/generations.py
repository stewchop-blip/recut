"""Charge one completed render action, never source downloads or failed attempts."""
import asyncio
import hashlib
from aiogram import BaseMiddleware

from app.core.config import get_settings
from app.core.logging import get_logger
from app.database.session import db_manager
from app.services.generations import AllowanceRejected, current_run, reserve, refund_unfinished

logger = get_logger(__name__)
RENDER_ACTIONS = {'action:quick_prep', 'action:maximum_transform', 'action:versions',
                  'action:analyze_long', 'url:recut'}


class GenerationMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        settings = get_settings()
        exempt = {int(x.strip()) for x in settings.unlimited_telegram_ids.split(',') if x.strip().isdigit()}
        user = getattr(event, 'from_user', None)
        if (not settings.generation_limits_enabled or not user or user.id in exempt
                or getattr(event, 'data', '') not in RENDER_ACTIONS):
            return await handler(event, data)
        run_id = hashlib.sha256(str(event.id).encode()).hexdigest()
        try:
            async with db_manager.session() as session:
                await reserve(session, user.id, run_id, settings)
        except AllowanceRejected as exc:
            await event.answer(str(exc), show_alert=True)
            return
        except Exception:
            logger.exception('generation_reservation_failed', user_id=user.id)
            await event.answer('Не удалось проверить лимит. Попробуй чуть позже.', show_alert=True)
            return
        token = current_run.set(run_id)
        try:
            return await handler(event, data)
        finally:
            current_run.reset(token)
            async def release():
                try:
                    async with db_manager.session() as session:
                        await refund_unfinished(session, run_id)
                except Exception:
                    logger.exception('generation_refund_needs_reconciliation', request_id=run_id)
            await asyncio.shield(release())
