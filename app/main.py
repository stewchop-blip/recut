"""Recut bot entrypoint with webhook mode (production) and polling (dev)."""
import asyncio
from contextlib import suppress

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.types import BotCommand
from aiogram.webhook.aiohttp_server import SimpleRequestHandler
from aiohttp import web

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
        # TelegramRetryAfter / Flood control — non-fatal
        logger.warning("telegram_commands_skipped", error=str(e)[:80])


def _build_dispatcher() -> Dispatcher:
    dp = Dispatcher()
    dp.include_router(start.router)
    dp.include_router(text_input.router)
    dp.include_router(voice_select.router)
    dp.include_router(rewrite_flow.router)
    return dp


# ============================================================================
# Webhook (production) — aiohttp app
# ============================================================================

async def _on_webhook_startup(app: web.Application) -> None:
    """aiohttp startup hook — runs once."""
    settings = get_settings()
    bot: Bot = app["bot"]

    # 1. Initialize DB
    db_manager.initialize()

    # 2. Clean stale temp files
    try:
        from app.utils.temp import get_temp_manager
        cleaned = get_temp_manager().cleanup_stale(max_age_hours=1)
        if cleaned:
            logger.info("startup_cleanup_done", removed=cleaned)
    except Exception as e:
        logger.warning("startup_cleanup_failed", error=str(e)[:80])

    # 3. Set bot commands (non-fatal)
    await _safe_set_my_commands(bot)

    # 4. Register webhook with Telegram
    if settings.webhook_url:
        await bot.set_webhook(
            url=settings.webhook_url,
            secret_token=settings.webhook_secret,
            drop_pending_updates=True,  # drop old updates to avoid reprocessing
        )
        logger.info("webhook_set", url=settings.webhook_url)
    else:
        logger.warning("webhook_url_empty_check_RAILWAY_PUBLIC_DOMAIN")


async def _on_webhook_shutdown(app: web.Application) -> None:
    """aiohttp shutdown hook — runs once."""
    bot: Bot = app["bot"]
    with suppress(Exception):
        await bot.delete_webhook()
    with suppress(Exception):
        await db_manager.close()
    with suppress(Exception):
        await bot.session.close()


def create_webhook_app() -> web.Application:
    """Build aiohttp app for production webhook mode."""
    settings = get_settings()
    setup_logging()

    app = web.Application()

    bot = Bot(
        token=settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode="HTML"),
    )
    dp = _build_dispatcher()

    app["bot"] = bot
    app["dp"] = dp

    # Webhook handler
    handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=settings.webhook_secret,
    )
    handler.register(app, path=settings.webhook_path)

    # Health check (Railway)
    async def healthz(_: web.Request) -> web.Response:
        return web.Response(status=200, text="OK")
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/", healthz)

    # Lifecycle — single hook each
    app.on_startup.append(_on_webhook_startup)
    app.on_shutdown.append(_on_webhook_shutdown)

    return app


def run_webhook() -> None:
    """Run production webhook server."""
    settings = get_settings()
    app = create_webhook_app()
    web.run_app(app, host="0.0.0.0", port=settings.port)


# ============================================================================
# Polling (development)
# ============================================================================

async def run_polling() -> None:
    """Run in polling mode (no public domain required)."""
    settings = get_settings()
    setup_logging()

    bot = Bot(
        token=settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode="HTML"),
    )
    dp = _build_dispatcher()

    db_manager.initialize()
    await _safe_set_my_commands(bot)

    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        with suppress(Exception):
            await db_manager.close()
        with suppress(Exception):
            await bot.session.close()


# ============================================================================

def main() -> None:
    settings = get_settings()
    if settings.environment == "production" and settings.railway_public_domain:
        run_webhook()
    else:
        asyncio.run(run_polling())


if __name__ == "__main__":
    main()