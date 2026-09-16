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
# New: video-repurpose Jobs
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