"""Application configuration for the Recut video-repurpose bot.

Environment-driven via Pydantic Settings. All new keys have safe defaults
so the bot can boot in development without every Railway variable set.
"""
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

    # === Telegram ===
    telegram_bot_token: str = Field(..., description="Telegram Bot Token from @BotFather")

    # === Access control (whitelist) ===
    # Comma-separated list of telegram user IDs allowed to use the bot.
    # Empty / unset = bot rejects everyone (safe default during closed beta).
    allowed_telegram_user_ids: str = Field(
        default="",
        description="Comma-separated Telegram user IDs allowed to use the bot",
    )

    # === OpenRouter (used only for transcript analysis / clip selection) ===
    openrouter_api_key: str = Field(..., description="OpenRouter API key")
    clip_analysis_model: str = Field(
        default="google/gemini-2.5-flash",
        description="OpenRouter text model used to pick interesting moments",
    )
    openrouter_timeout_seconds: int = Field(
        default=60, ge=5, le=300, description="OpenRouter request timeout",
    )

    # === Whisper STT ===
    whisper_model: str = Field(
        default="small",
        description="faster-whisper model size: tiny | base | small | medium | large-v3",
    )
    whisper_device: str = Field(
        default="cpu",
        description="faster-whisper device: cpu | cuda",
    )
    whisper_compute_type: str = Field(
        default="int8",
        description="faster-whisper compute type: int8 | int8_float16 | float16 | float32",
    )
    whisper_language: str = Field(
        default="ru",
        description="Primary transcription language (ISO 639-1). Empty = auto.",
    )

    # === Database ===
    database_url: str = Field(..., description="PostgreSQL async connection string")

    # === Telegram transport ===
    webhook_secret: str = Field(
        default="",
        description="Optional secret token for Telegram webhook validation",
    )
    railway_public_domain: str = Field(
        default="",
        description="Railway public domain (e.g. recut-production.up.railway.app)",
    )
    webhook_path: str = Field(default="/webhook", description="Webhook endpoint path")
    port: int = Field(default=8080, ge=1, le=65535)

    # === Video pipeline limits ===
    max_video_size_mb: int = Field(default=1500, ge=1, le=10000, description="Max upload MB")
    max_video_duration_minutes: int = Field(default=60, ge=1, le=600, description="Max duration")
    default_clip_count: int = Field(default=3, ge=1, le=10)
    max_clip_count: int = Field(default=5, ge=1, le=20)
    max_concurrent_jobs: int = Field(default=1, ge=1, le=4)
    clip_min_seconds: float = Field(default=20.0, ge=5.0, le=60.0)
    clip_max_seconds: float = Field(default=60.0, ge=10.0, le=180.0)
    clip_start_padding_seconds: float = Field(default=0.5, ge=0.0, le=5.0)
    clip_end_padding_seconds: float = Field(default=0.5, ge=0.0, le=5.0)

    # === Output video format ===
    output_width: int = Field(default=1080, ge=320, le=2160)
    output_height: int = Field(default=1920, ge=320, le=3840)
    output_fps: int = Field(default=30, ge=15, le=60)
    output_video_bitrate: str = Field(default="4M", description="e.g. 2M, 4M, 6M")
    output_audio_bitrate: str = Field(default="128k", description="e.g. 96k, 128k, 192k")

    # === CTA overlay ===
    cta_enabled: bool = Field(default=False, description="Show CTA banner overlay")
    cta_asset_path: str = Field(default="", description="Path to PNG/WebP with alpha")
    cta_mode: Literal["off", "full", "start", "end", "range"] = Field(default="end")
    cta_position: Literal[
        "top", "bottom", "top_left", "top_right", "bottom_left", "bottom_right",
    ] = Field(default="bottom")
    cta_start_seconds: float = Field(default=0.0, ge=0.0)
    cta_duration_seconds: float = Field(default=4.0, ge=0.5, le=60.0)
    cta_min_margin_px: int = Field(default=120, ge=0, le=600, description="Safe margin from edges")

    # === Subtitle safe areas (px) ===
    subtitle_safe_top_px: int = Field(default=300, ge=0)
    subtitle_safe_bottom_px: int = Field(default=520, ge=0)
    subtitle_safe_left_px: int = Field(default=80, ge=0)
    subtitle_safe_right_px: int = Field(default=80, ge=0)
    subtitle_max_words_per_line: int = Field(default=4, ge=1, le=10)

    # === Storage / cleanup ===
    temp_dir: str = Field(default="/tmp/recut", description="Job workspace root")
    job_ttl_minutes: int = Field(default=120, ge=1, description="Auto-cleanup older jobs")

    # === App ===
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(default="INFO")
    environment: Literal["development", "production"] = Field(default="production")

    # === Computed properties ===
    @property
    def webhook_url(self) -> str:
        if not self.railway_public_domain:
            return ""
        return f"https://{self.railway_public_domain}{self.webhook_path}"

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def allowed_user_id_set(self) -> set[int]:
        """Parse allowed_telegram_user_ids into a set of ints."""
        out: set[int] = set()
        for chunk in self.allowed_telegram_user_ids.split(","):
            chunk = chunk.strip()
            if chunk.isdigit():
                out.add(int(chunk))
        return out

    # === Validators ===
    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, v: str) -> str:
        if not v.startswith(("postgresql+asyncpg://", "postgresql://")):
            raise ValueError("DATABASE_URL must be postgresql+asyncpg:// or postgresql://")
        if v.startswith("postgresql://"):
            return v.replace("postgresql://", "postgresql+asyncpg://", 1)
        return v

    @field_validator("telegram_bot_token", "openrouter_api_key")
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

    @field_validator("clip_min_seconds", "clip_max_seconds")
    @classmethod
    def validate_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("Must be positive")
        return v


# Global settings instance (loaded once)
if not os.getenv("TELEGRAM_BOT_TOKEN"):
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        pass

settings = Settings()  # type: ignore[call-arg]


def get_settings() -> Settings:
    return settings
