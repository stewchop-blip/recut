"""Database models.

`User` and `Generation` are legacy from the old TTS bot — kept untouched
so we don't have to migrate existing data on Railway PostgreSQL.

`Job` is the new model used by the video-repurpose pipeline. Each Job
represents one source video that the user submitted for processing.
We deliberately do NOT store the video itself or its transcript —
only metadata so we can show recent activity and avoid double-submit.
"""
import enum
from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, DateTime, Enum, Float, Index, Integer, String, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Legacy (do not delete — preserves old TTS data on Railway PostgreSQL)
# ---------------------------------------------------------------------------

class GenerationType(str, enum.Enum):
    TTS = "tts"
    REWRITE = "rewrite"


class GenerationStatus(str, enum.Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"


class User(Base):
    """Telegram user (legacy)."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True, nullable=False)
    username: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    language_code: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_active_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class Generation(Base):
    """Generation job record (legacy TTS)."""

    __tablename__ = "generations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    type: Mapped[GenerationType] = mapped_column(Enum(GenerationType), nullable=False)
    input_length: Mapped[int] = mapped_column(Integer, nullable=False)
    voice: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    provider: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    model: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    status: Mapped[GenerationStatus] = mapped_column(Enum(GenerationStatus), default=GenerationStatus.PENDING, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)


# ---------------------------------------------------------------------------
# New: video-repurpose jobs
# ---------------------------------------------------------------------------

class JobStatus(str, enum.Enum):
    """Status of one source-video processing run."""
    PENDING = "pending"          # accepted, queued
    DOWNLOADING = "downloading"  # downloading from Telegram
    PROBING = "probing"          # ffprobe validation
    TRANSCRIBING = "transcribing"
    ANALYZING = "analyzing"      # OpenRouter clip selection
    CUTTING = "cutting"          # FFmpeg clip cut
    RENDERING = "rendering"      # 9:16 + subtitles + CTA + normalize
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Job(Base):
    """One source-video processing job.

    Stores only metadata — never the video or transcript itself.
    """

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Telegram linkage
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    telegram_chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status_message_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)

    # Source file (downloaded to local temp dir; we keep the path only
    # for the lifetime of the job, then drop it).
    source_filename: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    source_bytes: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    source_duration_seconds: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    source_width: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    source_height: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Pipeline bookkeeping
    clips_generated: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus), default=JobStatus.PENDING, nullable=False, index=True,
    )
    error_code: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    error_detail: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    # Wall-clock timestamps (UTC)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True,
    )
    processing_started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    processing_completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_jobs_user_created", "telegram_user_id", "created_at"),
        Index("ix_jobs_status_created", "status", "created_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<Job(id={self.id}, user={self.telegram_user_id}, "
            f"status={self.status.value}, clips={self.clips_generated})>"
        )


# ---------------------------------------------------------------------------
# New: per-user settings (CTA, subtitles, output mode)
# ---------------------------------------------------------------------------

class UserSettings(Base):
    """Per-Telegram-user preferences.

    One row per user. Settings persist across sessions — the user sets
    them once via 'Settings' menu and Quick Prep uses them automatically.
    """

    __tablename__ = "user_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_user_id: Mapped[int] = mapped_column(
        BigInteger, unique=True, index=True, nullable=False,
    )

    # CTA overlay
    cta_enabled: Mapped[bool] = mapped_column(default=False, nullable=False)
    cta_asset_path: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    cta_telegram_file_id: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    cta_position: Mapped[str] = mapped_column(String(20), default="bottom", nullable=False)
    cta_mode: Mapped[str] = mapped_column(String(20), default="end", nullable=False)
    cta_duration_seconds: Mapped[float] = mapped_column(default=4.0, nullable=False)
    cta_start_seconds: Mapped[float] = mapped_column(default=0.0, nullable=False)
    # Banner size preset: small / medium / large → max width fraction of frame
    cta_size: Mapped[str] = mapped_column(String(10), default="medium", nullable=False)
    # Universal overlay asset (Этап 3): png / webp / gif / mp4.
    # The stored file_id is type-agnostic; these two columns tell the
    # renderer how to overlay it (static vs looping animation).
    overlay_type: Mapped[str] = mapped_column(String(10), default="png", nullable=False)
    overlay_is_animated: Mapped[bool] = mapped_column(default=False, nullable=False)

    # Этап 4: template-based processing
    background_id: Mapped[str] = mapped_column(String(20), default="blur", nullable=False)
    title_id: Mapped[str] = mapped_column(String(20), default="none", nullable=False)
    brand_corner: Mapped[bool] = mapped_column(default=False, nullable=False)
    style_id: Mapped[str] = mapped_column(String(20), default="clean", nullable=False)
    # PHASE D: user-defined title text (used when title_id == "custom")
    custom_title: Mapped[str] = mapped_column(String(200), default="", nullable=False)

    # Subtitles (default OFF — quick prep does NOT run Whisper automatically)
    subtitles_enabled: Mapped[bool] = mapped_column(default=False, nullable=False)

    # Output mode (always universal_9_16 for MVP)
    output_mode: Mapped[str] = mapped_column(
        String(20), default="universal_9_16", nullable=False,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    def __repr__(self) -> str:
        return (
            f"<UserSettings(user={self.telegram_user_id}, cta_enabled={self.cta_enabled}, "
            f"position={self.cta_position}, mode={self.cta_mode})>"
        )