"""Best-effort private-chat measurement after access checks, before business handlers."""
import asyncio

from aiogram import BaseMiddleware, types

from app.core.logging import get_logger
from app.database.session import db_manager
from app.services.analytics import observe

logger = get_logger(__name__)


class AnalyticsMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = getattr(event, 'from_user', None)
        message = event if isinstance(event, types.Message) else getattr(event, 'message', None)
        if user and not user.is_bot and message and message.chat.type == 'private':
            # Receipts are service events, not a person returning to use the bot.
            receipt = isinstance(event, types.Message) and (event.successful_payment or event.refunded_payment)
            if not receipt:
                payload = ''
                text = (getattr(event, 'text', None) or '').split(maxsplit=1)
                if text and text[0].split('@')[0] == '/start' and len(text) == 2:
                    payload = text[1].strip()
                try:
                    async with asyncio.timeout(2):
                        async with db_manager.session() as session:
                            await observe(session, user.id, payload)
                except Exception:
                    # Analytics must never prevent a download, payment or render.
                    logger.warning('analytics_observation_failed')
        return await handler(event, data)
