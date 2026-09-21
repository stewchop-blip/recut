"""Recut bot entrypoint — Telegram-only video-repurpose bot.

Polling is the default (works on any Railway plan). Webhook mode is
available by setting WEBHOOK_MODE=true with a public domain.
"""
import asyncio
import os
from contextlib import suppress

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties

from app.bot.handlers import start, video
from app.bot.middlewares.access import AccessMiddleware
from app.core.config import get_settings
from app.core.logging import setup_logging, get_logger
from app.database.session import db_manager

logger = get_logger(__name__)


def _build_dispatcher() -> Dispatcher:
    dp = Dispatcher()
    # Whitelist gate runs before any router logic.
    dp.message.middleware.register(AccessMiddleware())
    dp.callback_query.middleware.register(AccessMiddleware())
    # Routers — start must be FIRST so commands like /start are handled
    # before the text/video handlers. We register video after start/help.
    dp.include_router(start.router)
    dp.include_router(video.router)
    return dp


async def _on_startup(bot: Bot) -> None:
    """Common startup: DB init + table creation, cleanup, commands."""
    db_manager.initialize()

    # Create tables if they don't exist (idempotent).
    # Without this the new `jobs` table is never created on Railway
    # and the first video crashes with UndefinedTableError.
    try:
        from app.database.models import Base
        async with db_manager.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("db_schema_ready")
    except Exception as e:
        logger.error("db_schema_init_failed", error=str(e)[:200])

    # Clean up stale Jobs left over from a previous process crash.
    # Without this a 'PENDING' Job from yesterday would block the user
    # from starting a new one (has_active_job ignores only terminal states).
    try:
        async with db_manager.session() as session:
            from app.database.repositories import JobRepository
            n = await JobRepository(session).cleanup_stale_jobs(max_age_minutes=30)
            if n:
                logger.info("stale_jobs_cleaned", count=n)
    except Exception as e:
        logger.warning("stale_jobs_cleanup_failed", error=str(e)[:200])

    # Clean stale job directories from previous runs
    try:
        from app.utils.temp import get_temp_manager
        cleaned = get_temp_manager().cleanup_stale(max_age_hours=2)
        if cleaned:
            logger.info("startup_cleanup_done", removed=cleaned)
    except Exception as e:
        logger.warning("startup_cleanup_failed", error=str(e)[:80])

    # Best-effort: register bot commands with Telegram. Wrapped because
    # redeploys trigger Flood control warnings if called too quickly.
    try:
        from aiogram.types import BotCommand
        await bot.set_my_commands([
            BotCommand(command="start", description="Начать"),
            BotCommand(command="help", description="Помощь"),
            BotCommand(command="cancel", description="Отменить"),
        ])
    except Exception as e:
        logger.warning("telegram_commands_skipped", error=str(e)[:80])


async def _on_shutdown(bot: Bot) -> None:
    with suppress(Exception):
        await db_manager.close()
    with suppress(Exception):
        await bot.session.close()


# ============================================================================
# Polling (default — works without public domain)
# ============================================================================

async def run_polling() -> None:
    settings = get_settings()
    setup_logging()

    bot = Bot(
        token=settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode="HTML"),
    )
    dp = _build_dispatcher()

    # Clear any leftover webhook from previous deployment
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
# Webhook (optional — only if WEBHOOK_MODE=true)
# ============================================================================

async def run_webhook() -> None:
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

    secret = settings.webhook_secret or None
    handler = SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=secret)
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
    use_webhook = os.getenv("WEBHOOK_MODE", "").lower() in ("1", "true", "yes")
    if use_webhook:
        run_webhook()
    else:
        asyncio.run(run_polling())


if __name__ == "__main__":
    main()