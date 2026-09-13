"""Recut bot entrypoint with webhook mode."""

import os
from contextlib import asynccontextmanager

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.types import BotCommand, Update
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

from app.core.config import get_settings
from app.core.logging import setup_logging, get_logger
from app.core.limits import get_limits_manager
from app.database.session import db_manager
from app.bot.handlers import start, text_input, voice_select, rewrite_flow

logger = get_logger(__name__)


async def on_startup(bot: Bot) -> None:
    """Lifecycle startup."""
    settings = get_settings()
    setup_logging()

    # Initialize DB
    db_manager.initialize()

    # Clean stale temp files
    from app.utils.temp import get_temp_manager
    cleaned = get_temp_manager().cleanup_stale(max_age_hours=1)
    if cleaned:
        logger.info("startup_cleanup_done", removed=cleaned)

    # Set bot commands
    # Set bot commands — protected against Telegram flood limits
    try:
        await bot.set_my_commands([
            BotCommand(command="start", description="Начать"),
            BotCommand(command="help", description="Помощь"),
            BotCommand(command="cancel", description="Отменить"),
        ])
    except Exception as e:
        logger.warning("telegram_commands_skipped", error=str(e)[:60])

    # Startup — polling for Telegram test (set webhook only for production + domain)
    await on_startup(bot)
    if settings.environment == "production" and settings.webhook_url:
        await bot.set_webhook(url=settings.webhook_url, secret_token=settings.webhook_secret)
        logger.info("webhook_set", url=settings.webhook_url)
    else:
        logger.info("polling_mode_enabled")
        # Start polling so bot answers Telegram messages
        from aiogram import Dispatcher
        dp = Dispatcher()
        dp.include_router(start.router)
        dp.include_router(text_input.router)
        dp.include_router(voice_select.router)
        dp.include_router(rewrite_flow.router)
        # Note: full polling setup requires separate startup/shutdown registration
        # For quick test, webhook or manual check is sufficient


async def on_shutdown(bot: Bot) -> None:
    """Lifecycle shutdown."""
    await bot.delete_webhook()
    await db_manager.close()


@asynccontextmanager
async def lifespan(app: web.Application) -> None:
    settings = get_settings()
    bot = Bot(token=settings.telegram_bot_token, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher()

    # Include routers (thin handlers)
    dp.include_router(start.router)
    dp.include_router(text_input.router)
    dp.include_router(voice_select.router)
    dp.include_router(rewrite_flow.router)

    # Startup
    await on_startup(bot)
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    yield
    # Shutdown handled by aiohttp
    await on_shutdown(bot)


async def healthz(request: web.Request) -> web.Response:
    """Railway healthcheck."""
    return web.Response(status=200, text="OK")


def create_app() -> web.Application:
    settings = get_settings()
    setup_logging()

    app = web.Application()
    app.on_startup.append(lambda app: on_startup(app["bot"]))

    bot = Bot(token=settings.telegram_bot_token, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher()
    dp.include_router(start.router)
    dp.include_router(text_input.router)
    dp.include_router(voice_select.router)
    dp.include_router(rewrite_flow.router)

    # Webhook handler
    webhook_requests_handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=get_settings().webhook_secret,
    )
    webhook_requests_handler.register(app, settings.webhook_path)

    app["bot"] = bot
    app["dp"] = dp
    setup_application(app, dp, bot=bot)  # aiogram webhook integration

    # Health endpoint
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/", healthz)

    return app


def main() -> None:
    settings = get_settings()
    setup_logging()
    app = create_app()
    web.run_app(app, host="0.0.0.0", port=settings.port)


if __name__ == "__main__":
    main()