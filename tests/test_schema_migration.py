"""Tests for idempotent schema migration on startup."""
from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.main import run_schema_migrations
from app.database.session import db_manager


def test_schema_migration_adds_column_idempotent(tmp_path):
    """Verify that run_schema_migrations adds cta_telegram_file_id and is idempotent."""
    import asyncio
    db_file = tmp_path / "test_migration.db"
    db_url = f"sqlite+aiosqlite:///{db_file}"

    async def _run():
        # Create old table without cta_telegram_file_id
        engine = create_async_engine(db_url)
        async with engine.begin() as conn:
            await conn.execute(text("""
                CREATE TABLE user_settings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_user_id BIGINT UNIQUE NOT NULL,
                    cta_enabled BOOLEAN DEFAULT 0 NOT NULL,
                    cta_asset_path VARCHAR(500),
                    cta_position VARCHAR(20) DEFAULT 'bottom' NOT NULL,
                    cta_mode VARCHAR(20) DEFAULT 'end' NOT NULL,
                    cta_duration_seconds FLOAT DEFAULT 4.0 NOT NULL,
                    cta_start_seconds FLOAT DEFAULT 0.0 NOT NULL,
                    subtitles_enabled BOOLEAN DEFAULT 0 NOT NULL,
                    output_mode VARCHAR(20) DEFAULT 'universal_9_16' NOT NULL,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL
                )
            """))

            await conn.execute(text("INSERT INTO user_settings (telegram_user_id, cta_enabled, cta_asset_path) VALUES (10, 1, 'existing-banner')"))
            # Verify old schema lacks cta_telegram_file_id
            res = await conn.execute(text("PRAGMA table_info(user_settings)"))
            cols = [row[1] for row in res.fetchall()]
            assert "cta_telegram_file_id" not in cols

        # Point db_manager to our engine
        old_engine = db_manager._engine
        db_manager._engine = engine
        try:
            # Pass 1: Run migration -> column must be added
            await run_schema_migrations()
            async with engine.begin() as conn:
                res = await conn.execute(text("PRAGMA table_info(user_settings)"))
                cols = [row[1] for row in res.fetchall()]
                assert "cta_telegram_file_id" in cols
                assert "decoration_enabled" in cols
                assert {"logo_telegram_file_id", "logo_enabled", "recut_branding", "premium_until",
                        "output_quality", "processing_style", "subtitle_style", "subtitle_language"} <= set(cols)
                existing = (await conn.execute(text("SELECT cta_enabled, cta_asset_path, recut_branding, logo_enabled, premium_until FROM user_settings WHERE telegram_user_id=10"))).one()
                assert tuple(existing) == (1, 'existing-banner', 1, 0, None)

            # Pass 2: Run migration again -> must not fail
            await run_schema_migrations()
            async with engine.begin() as conn:
                res = await conn.execute(text("PRAGMA table_info(user_settings)"))
                cols = [row[1] for row in res.fetchall()]
                assert "cta_telegram_file_id" in cols
                assert "decoration_enabled" in cols
                assert {"logo_telegram_file_id", "logo_enabled", "recut_branding", "premium_until",
                        "output_quality", "processing_style", "subtitle_style", "subtitle_language"} <= set(cols)
                existing = (await conn.execute(text("SELECT cta_enabled, cta_asset_path, recut_branding, logo_enabled, premium_until FROM user_settings WHERE telegram_user_id=10"))).one()
                assert tuple(existing) == (1, 'existing-banner', 1, 0, None)
        finally:
            db_manager._engine = old_engine
            await engine.dispose()

    asyncio.run(_run())
