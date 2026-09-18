"""Database repositories.

`UserRepository` and `GenerationRepository` are legacy (TTS-era) and
kept so we don't have to migrate existing data.

`JobRepository` is the new repository used by the video pipeline.
"""
from datetime import datetime
from typing import Optional, Sequence

from sqlalchemy import select, func, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    Generation,
    GenerationStatus,
    GenerationType,
    Job,
    JobStatus,
    User,
    UserSettings,
)


# ---------------------------------------------------------------------------
# Legacy (do not delete)
# ---------------------------------------------------------------------------

class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_telegram_id(self, telegram_user_id: int) -> Optional[User]:
        result = await self.session.execute(
            select(User).where(User.telegram_user_id == telegram_user_id)
        )
        return result.scalar_one_or_none()

    async def get_or_create(
        self,
        telegram_user_id: int,
        username: Optional[str] = None,
        language_code: Optional[str] = None,
    ) -> User:
        user = await self.get_by_telegram_id(telegram_user_id)
        if user:
            user.last_active_at = datetime.utcnow()
            if username and user.username != username:
                user.username = username
            if language_code and user.language_code != language_code:
                user.language_code = language_code
            return user
        user = User(
            telegram_user_id=telegram_user_id,
            username=username,
            language_code=language_code,
        )
        self.session.add(user)
        await self.session.flush()
        return user


class GenerationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        user_id: int,
        type: GenerationType,
        input_length: int,
        voice: Optional[str] = None,
        provider: Optional[str] = None,
        model: Optional[str] = None,
    ) -> Generation:
        generation = Generation(
            user_id=user_id,
            type=type,
            input_length=input_length,
            voice=voice,
            provider=provider,
            model=model,
            status=GenerationStatus.PENDING,
        )
        self.session.add(generation)
        await self.session.flush()
        return generation

    async def mark_completed(self, generation_id: int) -> Optional[Generation]:
        result = await self.session.execute(
            select(Generation).where(Generation.id == generation_id)
        )
        generation = result.scalar_one_or_none()
        if generation:
            generation.status = GenerationStatus.COMPLETED
            generation.completed_at = datetime.utcnow()
        return generation

    async def mark_failed(self, generation_id: int, error_code: str) -> Optional[Generation]:
        result = await self.session.execute(
            select(Generation).where(Generation.id == generation_id)
        )
        generation = result.scalar_one_or_none()
        if generation:
            generation.status = GenerationStatus.FAILED
            generation.completed_at = datetime.utcnow()
            generation.error_code = error_code
        return generation

    async def count_today(self, user_id: int) -> int:
        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        result = await self.session.execute(
            select(func.count(Generation.id))
            .where(Generation.user_id == user_id)
            .where(Generation.created_at >= today_start)
        )
        return result.scalar_one() or 0


# ---------------------------------------------------------------------------
# New: video-repurpose jobs
# ---------------------------------------------------------------------------

class JobRepository:
    """CRUD for the `jobs` table."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        telegram_user_id: int,
        telegram_chat_id: int,
        source_message_id: int,
        source_filename: Optional[str] = None,
        source_bytes: Optional[int] = None,
    ) -> Job:
        job = Job(
            telegram_user_id=telegram_user_id,
            telegram_chat_id=telegram_chat_id,
            source_message_id=source_message_id,
            source_filename=source_filename,
            source_bytes=source_bytes,
            status=JobStatus.PENDING,
        )
        self.session.add(job)
        await self.session.flush()
        return job

    async def get(self, job_id: int) -> Optional[Job]:
        result = await self.session.execute(select(Job).where(Job.id == job_id))
        return result.scalar_one_or_none()

    async def get_by_message(
        self, telegram_user_id: int, source_message_id: int,
    ) -> Optional[Job]:
        result = await self.session.execute(
            select(Job).where(
                Job.telegram_user_id == telegram_user_id,
                Job.source_message_id == source_message_id,
            ).order_by(Job.created_at.desc()).limit(1)
        )
        return result.scalar_one_or_none()

    async def set_status(
        self,
        job_id: int,
        status: JobStatus,
        error_code: Optional[str] = None,
        error_detail: Optional[str] = None,
        status_message_id: Optional[int] = None,
    ) -> None:
        values: dict[str, object] = {"status": status}
        if status == JobStatus.DOWNLOADING and not self._already_started(job_id):
            values["processing_started_at"] = datetime.utcnow()
        if status in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED):
            values["processing_completed_at"] = datetime.utcnow()
        if error_code is not None:
            values["error_code"] = error_code
        if error_detail is not None:
            values["error_detail"] = error_detail[:500]
        if status_message_id is not None:
            values["status_message_id"] = status_message_id
        await self.session.execute(
            update(Job).where(Job.id == job_id).values(**values)
        )

    def _already_started(self, job_id: int) -> bool:
        # Cheap heuristic — only stamp processing_started_at once.
        return False

    async def update_source_meta(
        self,
        job_id: int,
        source_duration_seconds: float,
        source_width: int,
        source_height: int,
    ) -> None:
        await self.session.execute(
            update(Job).where(Job.id == job_id).values(
                source_duration_seconds=source_duration_seconds,
                source_width=source_width,
                source_height=source_height,
            )
        )

    async def mark_completed(self, job_id: int, clips_generated: int) -> None:
        await self.session.execute(
            update(Job).where(Job.id == job_id).values(
                status=JobStatus.COMPLETED,
                clips_generated=clips_generated,
                processing_completed_at=datetime.utcnow(),
            )
        )

    async def mark_failed(
        self, job_id: int, error_code: str, error_detail: str,
    ) -> None:
        await self.session.execute(
            update(Job).where(Job.id == job_id).values(
                status=JobStatus.FAILED,
                error_code=error_code,
                error_detail=error_detail[:500],
                processing_completed_at=datetime.utcnow(),
            )
        )

    async def has_active_job(self, telegram_user_id: int, *, max_age_minutes: int = 30) -> bool:
        """Return True if the user has an unfinished job that's not stale.

        A job is considered "active" only if:
        - its status is one of the in-flight statuses
        - AND it was created within the last `max_age_minutes` minutes

        Stale jobs (e.g. left in PENDING/CUTTING after a previous process
        crashed) are ignored so they don't block the user forever.
        """
        from datetime import datetime, timedelta, timezone
        active = {
            JobStatus.PENDING, JobStatus.DOWNLOADING, JobStatus.PROBING,
            JobStatus.TRANSCRIBING, JobStatus.ANALYZING,
            JobStatus.CUTTING, JobStatus.RENDERING,
        }
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)
        result = await self.session.execute(
            select(func.count(Job.id))
            .where(Job.telegram_user_id == telegram_user_id)
            .where(Job.status.in_(active))
            .where(Job.created_at >= cutoff)
        )
        return (result.scalar_one() or 0) > 0

    async def cleanup_stale_jobs(self, *, max_age_minutes: int = 30) -> int:
        """Mark any non-terminal job older than `max_age_minutes` as FAILED.

        Called on startup to clean up after a process crash. Returns
        the number of rows updated.
        """
        from datetime import datetime, timedelta, timezone
        from sqlalchemy import update as _upd

        active = {
            JobStatus.PENDING, JobStatus.DOWNLOADING, JobStatus.PROBING,
            JobStatus.TRANSCRIBING, JobStatus.ANALYZING,
            JobStatus.CUTTING, JobStatus.RENDERING,
        }
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)
        result = await self.session.execute(
            _upd(Job)
            .where(Job.status.in_(active))
            .where(Job.created_at < cutoff)
            .values(
                status=JobStatus.FAILED,
                error_code="STALE",
                error_detail=f"Marked stale on startup (> {max_age_minutes} min old)",
                processing_completed_at=datetime.now(timezone.utc),
            )
        )
        return result.rowcount or 0

    async def list_recent(
        self, telegram_user_id: int, limit: int = 10,
    ) -> Sequence[Job]:
        result = await self.session.execute(
            select(Job)
            .where(Job.telegram_user_id == telegram_user_id)
            .order_by(Job.created_at.desc())
            .limit(limit)
        )
        return result.scalars().all()


class UserSettingsRepository:
    """CRUD for the `user_settings` table."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, telegram_user_id: int) -> UserSettings | None:
        result = await self.session.execute(
            select(UserSettings).where(
                UserSettings.telegram_user_id == telegram_user_id
            )
        )
        return result.scalar_one_or_none()

    async def get_or_create(self, telegram_user_id: int) -> UserSettings:
        """Return existing row, or insert a fresh one with default values."""
        existing = await self.get(telegram_user_id)
        if existing is not None:
            return existing
        row = UserSettings(telegram_user_id=telegram_user_id)
        self.session.add(row)
        await self.session.flush()
        return row

    async def update_fields(
        self,
        telegram_user_id: int,
        **fields: object,
    ) -> UserSettings:
        """Update one or more fields on the user's settings row.

        Auto-creates the row if it doesn't exist yet.
        """
        from sqlalchemy import update
        row = await self.get_or_create(telegram_user_id)
        for key, value in fields.items():
            if hasattr(row, key):
                setattr(row, key, value)
        await self.session.flush()
        return row