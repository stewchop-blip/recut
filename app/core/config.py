"""Application configuration using Pydantic Settings."""

import os
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=Path(__file__).parent.parent.parent / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Telegram ---
    telegram_bot_token: str = Field(..., description="Telegram Bot Token from @BotFather")

    # --- OpenRouter ---
    openrouter_api_key: str = Field(..., description="OpenRouter API key (dedicated for Recut)")
    openrouter_tts_model: str = Field(
        default="google/gemini-2.5-flash-preview-tts",
        description="OpenRouter model ID for TTS (Gemini TTS, free tier)",
    )
    openrouter_rewrite_model: str = Field(
        default="anthropic/claude-3-haiku",
        description="OpenRouter model ID for text rewriting",
    )

    # --- Database ---
    database_url: str = Field(..., description="PostgreSQL async connection string")

    # --- Webhook ---
    webhook_secret: str = Field(..., description="Secret token for Telegram webhook validation")
    railway_public_domain: str = Field(
        default="",
        description="Railway public domain (e.g. recut-production.up.railway.app)",
    )
    webhook_path: str = Field(default="/webhook", description="Webhook endpoint path")

    # --- Limits ---
    max_text_length: int = Field(default=4000, ge=1, le=10000, description="Max characters per text input")
    max_generations_per_day: int = Field(default=20, ge=1, le=1000, description="Daily generation limit per user")
    tts_timeout_seconds: int = Field(default=30, ge=5, le=300, description="TTS API timeout")
    rewrite_timeout_seconds: int = Field(default=20, ge=5, le=300, description="Rewrite API timeout")

    # --- App ---
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(default="INFO")
    environment: Literal["development", "production"] = Field(default="development")
    port: int = Field(default=8080, ge=1, le=65535)

    # --- Computed properties ---
    @property
    def webhook_url(self) -> str:
        """Full webhook URL for Telegram."""
        if not self.railway_public_domain:
            return ""
        return f"https://{self.railway_public_domain}{self.webhook_path}"

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, v: str) -> str:
        if not v.startswith(("postgresql+asyncpg://", "postgresql://")):
            raise ValueError("DATABASE_URL must be postgresql+asyncpg:// or postgresql://")
        # Ensure asyncpg driver
        if v.startswith("postgresql://"):
            return v.replace("postgresql://", "postgresql+asyncpg://", 1)
        return v

    @field_validator("telegram_bot_token", "openrouter_api_key", "webhook_secret")
    @classmethod
    def validate_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("Value cannot be empty")
        return v.strip()

    @field_validator("webhook_path")
    @classmethod
    def validate_webhook_path(cls, v: str) -> str:
        if not v.startswith("/"):
            return f"/{v}"
        return v


# Global settings instance (loaded once)
# Fallback to .env if Railway Variables empty (local/backup)
if not os.getenv("TELEGRAM_BOT_TOKEN"):
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        pass

settings = Settings()  # type: ignore[call-arg]


def get_settings() -> Settings:
    """Dependency injection friendly getter."""
    return settings