"""Bounded render admission for every account, including quota-exempt testers."""
import asyncio

from aiogram import BaseMiddleware

from app.bot.middlewares.generations import RENDER_ACTIONS
from app.database.repositories import UserSettingsRepository
from app.database.session import db_manager
from app.services.appearance import branding_required, effective_branding
from app.core.logging import get_logger

logger = get_logger(__name__)
active_render_users: set[int] = set()


class RenderQueueMiddleware(BaseMiddleware):
    def __init__(self):
        self._users = active_render_users
        self._semaphore = asyncio.Semaphore(1)

    async def __call__(self, handler, event, data):
        user = getattr(event, 'from_user', None)
        action = getattr(event, 'data', '')
        # Original delivery uses no compute, but must not race an active render.
        if not user or action not in RENDER_ACTIONS | {'url:original'}:
            return await handler(event, data)
        if user.id in self._users:
            await event.answer('Видео уже обрабатывается. Дождись результата.', show_alert=True)
            return
        if len(self._users) >= 8:
            await event.answer('Очередь заполнена. Попробуй чуть позже.', show_alert=True)
            return
        self._users.add(user.id)
        token = None
        try:
            if action == 'url:original':
                return await handler(event, data)
            async with db_manager.session() as session:
                s = await UserSettingsRepository(session).get_or_create(user.id)
                enabled = effective_branding(s)
            token = branding_required.set(enabled)
            if self._semaphore.locked() and event.message:
                await event.answer('Ролик в очереди…')
                await event.message.edit_text('⏳ Ролик в очереди. Скоро начнём обработку.')
            async with self._semaphore:
                return await handler(event, data)
        finally:
            if token is not None:
                branding_required.reset(token)
            self._users.discard(user.id)
