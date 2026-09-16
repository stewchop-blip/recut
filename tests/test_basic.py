"""Lightweight tests that don't touch real APIs.

The TTS-era tests (voice mapping, rewrite prompt) were removed when
TTS was dropped. New tests will be added per pipeline stage.
"""
import pytest

from app.core.config import Settings


def test_settings_loads_with_minimal_env():
    """Settings can be constructed with only the required vars."""
    s = Settings(
        telegram_bot_token="t",
        openrouter_api_key="k",
        database_url="postgresql+asyncpg://x",
    )
    assert s.telegram_bot_token == "t"
    assert s.openrouter_api_key == "k"
    assert s.environment in ("development", "production")


def test_database_url_is_normalised_to_asyncpg():
    """A plain postgresql:// URL must be rewritten to postgresql+asyncpg://."""
    s = Settings(
        telegram_bot_token="t",
        openrouter_api_key="k",
        database_url="postgresql://u:p@h/d",
    )
    assert s.database_url.startswith("postgresql+asyncpg://")


def test_allowed_user_id_set_parses_correctly():
    s = Settings(
        telegram_bot_token="t",
        openrouter_api_key="k",
        database_url="postgresql+asyncpg://x",
        allowed_telegram_user_ids="111, 222 ,333",
    )
    assert s.allowed_user_id_set == {111, 222, 333}


def test_allowed_user_id_set_empty_when_unset():
    s = Settings(
        telegram_bot_token="t",
        openrouter_api_key="k",
        database_url="postgresql+asyncpg://x",
    )
    assert s.allowed_user_id_set == set()


def test_webhook_path_normalised_to_leading_slash():
    s = Settings(
        telegram_bot_token="t",
        openrouter_api_key="k",
        database_url="postgresql+asyncpg://x",
        webhook_path="webhook",
    )
    assert s.webhook_path == "/webhook"


def test_temp_dir_default():
    s = Settings(
        telegram_bot_token="t",
        openrouter_api_key="k",
        database_url="postgresql+asyncpg://x",
    )
    assert s.temp_dir == "/tmp/recut"


def test_temp_manager_init():
    """The job workspace directory should exist after first init."""
    from app.utils.temp import TempFileManager
    tm = TempFileManager()
    assert tm.BASE_DIR.exists()
    assert tm.BASE_DIR.is_dir()