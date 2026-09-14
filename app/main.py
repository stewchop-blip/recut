"""Recut bot entrypoint — polling mode (primary), webhook fallback.

Polling is the default for Railway: it works without a public domain,
doesn't fight Telegram Flood limits on setWebhook, and survives redeploys.
Webhook mode is only used if WEBHOOK_MODE=true is explicitly set.
"""
import asyncio
import os
from contextlib import suppress

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.types import BotCommand

from app.core.config import get_settings
from app.core.logging import setup_logging, get_logger
from app.database.session import db_manager
from app.bot.handlers import start, text_input, voice_select, rewrite_flow

logger = get_logger(__name__)


async def _safe_set_my_commands(bot: Bot) -> None:
    """Set bot menu commands; ignore Flood control limits."""
    try:
        await bot.set_my_commands([
            BotCommand(command="start", description="Начать"),
            BotCommand(command="help", description="Помощь"),
            BotCommand(command="cancel", description="Отменить"),
        ])
    except Exception as e:
        logger.warning("telegram_commands_skipped", error=str(e)[:80])


def _build_dispatcher() -> Dispatcher:
    # Plain Dispatcher — we keep cross-handler state in an in-memory
    # UserTextStore (see app.bot.user_text_store) instead of FSM, because
    # MemoryStorage was dropping state between updates.
    dp = Dispatcher()
    dp.include_router(start.router)
    dp.include_router(text_input.router)
    dp.include_router(voice_select.router)
    dp.include_router(rewrite_flow.router)
    return dp


async def _on_startup(bot: Bot) -> None:
    """Common startup: DB init, cleanup, commands."""
    settings = get_settings()

    # Initialize DB
    db_manager.initialize()

    # Clean stale temp files
    try:
        from app.utils.temp import get_temp_manager
        cleaned = get_temp_manager().cleanup_stale(max_age_hours=1)
        if cleaned:
            logger.info("startup_cleanup_done", removed=cleaned)
    except Exception as e:
        logger.warning("startup_cleanup_failed", error=str(e)[:80])

    # Set bot commands (non-fatal — Telegram floods us otherwise)
    await _safe_set_my_commands(bot)


async def _on_shutdown(bot: Bot) -> None:
    """Common shutdown: close DB, close bot session."""
    with suppress(Exception):
        await db_manager.close()
    with suppress(Exception):
        await bot.session.close()


# ============================================================================
# Polling mode (default — robust, no domain required)
# ============================================================================

async def run_polling() -> None:
    """Long-polling mode. Works on any host."""
    settings = get_settings()
    setup_logging()

    bot = Bot(
        token=settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode="HTML"),
    )
    dp = _build_dispatcher()

    # CRITICAL: clear any leftover webhook from previous deployment
    # (otherwise Telegram returns Conflict and bot stays silent)
    with suppress(Exception):
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("webhook_cleared_for_polling")

    await _on_startup(bot)

    try:
        logger.info("polling_started", bot_id=bot.id)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await _on_shutdown(bot)


# ============================================================================
# Webhook mode (optional — only if WEBHOOK_MODE=true)
# ============================================================================

async def run_webhook() -> None:
    """Webhook mode — requires WEBHOOK_URL env var."""
    from aiogram.webhook.aiohttp_server import SimpleRequestHandler
    from aiohttp import web

    settings = get_settings()
    setup_logging()

    bot = Bot(
        token=settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode="HTML"),
    )
    dp = _build_dispatcher()

    app = web.Application()
    app["bot"] = bot
    app["dp"] = dp

    handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=settings.webhook_secret,
    )
    handler.register(app, path=settings.webhook_path)

    async def healthz(_: web.Request) -> web.Response:
        return web.Response(status=200, text="OK")
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/", healthz)

    app.on_startup.append(lambda _: _on_startup(bot))
    app.on_shutdown.append(lambda _: _on_shutdown(bot))

    logger.info("webhook_started", url=settings.webhook_url, port=settings.port)
    web.run_app(app, host="0.0.0.0", port=settings.port)


def main() -> None:
    """Decide webhook vs polling from env."""
    # WEBHOOK_MODE flag overrides everything; otherwise default to polling
    use_webhook = os.getenv("WEBHOOK_MODE", "").lower() in ("1", "true", "yes")

    if use_webhook:
        run_webhook()
    else:
        asyncio.run(run_polling())


if __name__ == "__main__":
    main()