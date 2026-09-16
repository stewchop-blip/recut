"""Access middleware — drops messages from non-whitelisted users.

If `ALLOWED_TELEGRAM_USER_IDS` is empty, the bot rejects every user
(safe default during closed beta). The rejection message is generic
and reveals nothing about the bot's internals.
"""
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

REJECTION_TEXT = "Бот пока работает в закрытом режиме."


class AccessMiddleware(BaseMiddleware):
    """Reject updates from users not in the whitelist."""

    def __init__(self) -> None:
        super().__init__()
        # Re-read each call so config edits in tests are honored
        self._cached_ids: set[int] | None = None

    def _allowed_ids(self) -> set[int]:
        return get_settings().allowed_user_id_set

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user_id = _extract_user_id(event)
        if user_id is None:
            # No user (e.g. channel post). Skip — not our concern.
            return await handler(event, data)

        allowed = self._allowed_ids()
        if not allowed:
            # Closed beta: silently drop if no whitelist configured.
            await _safe_reply(event, REJECTION_TEXT)
            logger.warning("access_denied_no_whitelist", user_id=user_id)
            return None

        if user_id not in allowed:
            await _safe_reply(event, REJECTION_TEXT)
            logger.info("access_denied", user_id=user_id)
            return None

        # Stash user_id for downstream handlers
        data["telegram_user_id"] = user_id
        return await handler(event, data)


def _extract_user_id(event: TelegramObject) -> int | None:
    """Pull `from_user.id` out of common aiogram event types."""
    user = getattr(event, "from_user", None)
    if user is not None and getattr(user, "id", None) is not None:
        return int(user.id)
    # CallbackQuery etc.
    if hasattr(event, "message") and getattr(event.message, "from_user", None):
        return int(event.message.from_user.id)  # type: ignore[union-attr]
    return None


async def _safe_reply(event: TelegramObject, text: str) -> None:
    """Send a one-off message back, swallowing any exception."""
    try:
        if isinstance(event, Message):
            await event.answer(text)
        elif hasattr(event, "message") and event.message is not None:  # type: ignore[union-attr]
            await event.message.answer(text)  # type: ignore[union-attr]
        elif hasattr(event, "answer"):  # CallbackQuery
            await event.answer(text, show_alert=True)  # type: ignore[attr-defined]
    except Exception as e:
        logger.warning("access_middleware_reply_failed", error=str(e)[:80])
