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


async def _migrate_jobstatus_enum() -> None:
    """PostgreSQL enum migration for jobs.status (HOTFIX 2026-09-26).

    create_all() does NOT extend an existing PG ENUM type. When JobStatus.READY
    was added to the Python enum, production kept the old labels and every
    transition to READY died with a DataError. Introspect the actual enum,
    log existing values, and idempotently add any missing labels — matching
    the existing casing (SQLAlchemy stores enum NAMES, e.g. PENDING).
    """
    from sqlalchemy import text
    if "sqlite" in str(db_manager.engine.url):
        return  # SQLite stores VARCHAR — nothing to migrate

    async with db_manager.engine.connect() as conn_raw:
        await conn_raw.execution_options(isolation_level="AUTOCOMMIT")
        # 1. Find the actual enum type backing jobs.status (do not guess).
        res = await conn_raw.execute(text(
            "SELECT udt_name FROM information_schema.columns "
            "WHERE table_name = 'jobs' AND column_name = 'status' LIMIT 1"))
        row = res.fetchone()
        typname = row[0] if row else None
        if not typname:
            logger.warning("jobstatus_enum_not_found", note="jobs.status missing?")
            return

        res = await conn_raw.execute(text(
            "SELECT e.enumlabel FROM pg_type t "
            "JOIN pg_enum e ON t.oid = e.enumtypid "
            "WHERE t.typname = :t ORDER BY e.enumsortorder"), {"t": typname})
        existing = [r[0] for r in res.fetchall()]
        logger.info("postgres_jobstatus_before", typname=typname, values=existing)

        # 2. Same casing as existing labels (NAMES by default in SQLAlchemy).
        wanted = ["PENDING", "READY", "DOWNLOADING", "PROBING", "TRANSCRIBING",
                  "ANALYZING", "CUTTING", "RENDERING", "COMPLETED", "FAILED",
                  "CANCELLED"]
        lowercase = any(v == v.lower() and v.isalpha() for v in existing)
        if lowercase:
            wanted = [w.lower() for w in wanted]
        missing = [w for w in wanted if w not in existing]
        if not missing:
            logger.info("postgres_jobstatus_ok", values=existing)
            return

        # 3. ALTER TYPE ... ADD VALUE IF NOT EXISTS (autocommit mode).
        for label in missing:
            await conn_raw.exec_driver_sql(
                f'ALTER TYPE "{typname}" ADD VALUE IF NOT EXISTS \'{label}\'')
            logger.info("postgres_jobstatus_value_added", typname=typname, value=label)

        res = await conn_raw.execute(text(
            "SELECT e.enumlabel FROM pg_type t "
            "JOIN pg_enum e ON t.oid = e.enumtypid "
            "WHERE t.typname = :t ORDER BY e.enumsortorder"), {"t": typname})
        after = [r[0] for r in res.fetchall()]
        logger.info("postgres_jobstatus_after", values=after)


async def _ready_persistence_selfcheck() -> None:
    """Startup self-check (HOTFIX item 14): JobStatus.READY must persist.

    Creates a throwaway Job row with READY, reads it back, deletes it.
    On failure the bot must NOT start — every upload would be broken.
    """
    from sqlalchemy import delete as _delete, select as _select
    from app.database.models import Job, JobStatus
    try:
        async with db_manager.session() as session:
            job = Job(telegram_user_id=0, telegram_chat_id=0,
                      source_message_id=0, status=JobStatus.READY)
            session.add(job)
            await session.flush()
            jid = job.id
            await session.commit()
        async with db_manager.session() as session:
            row = (await session.execute(
                _select(Job).where(Job.id == jid))).scalar_one()
            assert row.status == JobStatus.READY, f"read back {row.status}"
            await session.execute(_delete(Job).where(Job.id == jid))
            await session.commit()
        logger.info("db_ready_selfcheck_ok", job_id=jid)
    except Exception as e:
        logger.critical("DB SCHEMA INVALID: READY unsupported", error=str(e)[:300])
        raise


async def run_schema_migrations() -> None:
    """Run idempotent schema migrations on startup.
    
    SQLAlchemy's Base.metadata.create_all does not add new columns to
    pre-existing tables. We explicitly apply ALTER TABLE statements here.
    """
    from sqlalchemy import text
    # (column, SQL type) pairs added to user_settings over time.
    columns = [
        ("cta_telegram_file_id", "VARCHAR(200)"),
        ("cta_size", "VARCHAR(10)"),
        ("overlay_type", "VARCHAR(10) DEFAULT 'png'"),
        ("overlay_is_animated", "BOOLEAN DEFAULT FALSE"),
        ("background_id", "VARCHAR(20) DEFAULT 'blur'"),
        ("title_id", "VARCHAR(20) DEFAULT 'none'"),
        ("brand_corner", "BOOLEAN DEFAULT FALSE"),
        ("style_id", "VARCHAR(20) DEFAULT 'clean'"),
        ("custom_title", "VARCHAR(200) DEFAULT ''"),
        ("audio_preset", "VARCHAR(10) DEFAULT 'original'"),
        ("current_media_path", "VARCHAR(500) DEFAULT NULL"),
        ("current_media_job_id", "INTEGER DEFAULT NULL"),
        ("current_media_status", "VARCHAR(20) DEFAULT NULL"),
        ("current_media_source_type", "VARCHAR(20) DEFAULT NULL"),
        ("current_media_url", "VARCHAR(1000) DEFAULT NULL"),
        ("current_media_telegram_file_id", "VARCHAR(200) DEFAULT NULL"),
        ("current_media_normalized", "BOOLEAN DEFAULT FALSE"),
    ]
    is_sqlite = "sqlite" in str(db_manager.engine.url)
    try:
        async with db_manager.engine.begin() as conn:
            if is_sqlite:
                res = await conn.execute(text("PRAGMA table_info(user_settings)"))
                cols = [row[1] for row in res.fetchall()]
                for name, sqltype in columns:
                    if cols and name not in cols:
                        await conn.execute(text(f"ALTER TABLE user_settings ADD COLUMN {name} {sqltype}"))
                        logger.info("schema_migration_ok", dialect="sqlite", column=name)
            else:
                for name, sqltype in columns:
                    await conn.execute(text(f"ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS {name} {sqltype}"))
                    logger.info("schema_migration_ok", dialect="postgres", column=name)
    except Exception as e:
        logger.error("schema_migration_failed", error=str(e)[:200])


async def _on_startup(bot: Bot) -> None:
    """Common startup: DB init + table creation, cleanup, commands."""
    db_manager.initialize()

    # Create tables if they don't exist (idempotent).
    try:
        from app.database.models import Base
        async with db_manager.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("db_schema_ready")
    except Exception as e:
        logger.error("db_schema_init_failed", error=str(e)[:200])

    # Run schema migrations for existing tables
    await run_schema_migrations()
    # HOTFIX: extend the PostgreSQL jobs.status enum (READY etc.) —
    # create_all() does not alter existing enum types.
    try:
        await _migrate_jobstatus_enum()
    except Exception as e:
        logger.error("jobstatus_enum_migration_failed", error=str(e)[:300])

    # HOTFIX item 14: refuse to start unless READY actually persists.
    await _ready_persistence_selfcheck()

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