"""Database repositories."""

from datetime import datetime
from typing import Optional, Sequence

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import User, Generation, GenerationType, GenerationStatus


class UserRepository:
    """User repository."""

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
            # Update last_active_at and username if changed
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
    """Generation repository."""

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

    async def get_user_generations(
        self,
        user_id: int,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[Generation]:
        result = await self.session.execute(
            select(Generation)
            .where(Generation.user_id == user_id)
            .order_by(Generation.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return result.scalars().all()

    async def count_today(self, user_id: int) -> int:
        """Count generations for today (for quota)."""
        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        result = await self.session.execute(
            select(func.count(Generation.id))
            .where(Generation.user_id == user_id)
            .where(Generation.created_at >= today_start)
        )
        return result.scalar_one() or 0