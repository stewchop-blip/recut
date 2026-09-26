"""CurrentMedia service (audit Phase 1-13, donor: reels-downloader-bot).

Single source of truth for the user's currently loaded media — independent
of Telegram message state. Uses PostgreSQL (survives Railway redeploy) plus
optional Telegram file_id for cloud recovery.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

from app.core.logging import get_logger
from app.database.session import db_manager
from app.database.repositories import UserSettingsRepository

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class CurrentMedia:
    user_id: int
    job_id: int | None
    status: str             # READY / PROCESSING / COMPLETED / CANCELLED
    source_type: str        # url / telegram / file
    source_path: Path | None
    telegram_file_id: str | None
    source_url: str | None
    duration: float
    width: int
    height: int
    normalized: bool        # SourceNormalizer applied
    created_at: str         # ISO timestamp (simplified; DB stores real datetime)


class CurrentMediaService:
    """Minimal stable service for current user media context."""

    async def get(self, user_id: int) -> Optional[CurrentMedia]:
        async with db_manager.session() as session:
            repo = UserSettingsRepository(session)
            s = await repo.get(user_id)
            if s is None or not getattr(s, "current_media_path", None):
                # Try to recover from Telegram file_id if local file lost
                return None
            path = Path(s.current_media_path) if s.current_media_path else None
            return CurrentMedia(
                user_id=user_id,
                job_id=getattr(s, "current_media_job_id", None),
                status=getattr(s, "current_media_status", "READY"),
                source_type=getattr(s, "current_media_source_type", "unknown"),
                source_path=path,
                telegram_file_id=getattr(s, "current_media_telegram_file_id", None) or None,
                source_url=getattr(s, "current_media_url", None) or None,
                duration=getattr(s, "current_media_duration", 0.0),
                width=0,
                height=0,
                normalized=bool(getattr(s, "current_media_normalized", False)),
                created_at="",
            )

    async def set_ready(
        self, user_id: int, source_path: Path, job_id: int | None = None,
        telegram_file_id: str | None = None, source_url: str | None = None,
    ) -> CurrentMedia:
        async with db_manager.session() as session:
            await UserSettingsRepository(session).update_fields(
                user_id,
                current_media_path=str(source_path) if source_path else None,
                current_media_job_id=job_id,
                current_media_status="READY",
                current_media_source_type="telegram" if telegram_file_id else ("url" if source_url else "file"),
                current_media_url=source_url,
                current_media_telegram_file_id=telegram_file_id,
            )
        return CurrentMedia(
            user_id=user_id,
            job_id=job_id,
            status="READY",
            source_type="url" if source_url else ("telegram" if telegram_file_id else "file"),
            source_path=source_path,
            telegram_file_id=telegram_file_id,
            source_url=source_url,
            duration=0.0,
            width=0,
            height=0,
            normalized=False,
            created_at="",
        )

    async def clear(self, user_id: int) -> None:
        async with db_manager.session() as session:
            await UserSettingsRepository(session).update_fields(
                user_id,
                current_media_path=None,
                current_media_job_id=None,
                current_media_status=None,
                current_media_source_type=None,
                current_media_url=None,
                current_media_telegram_file_id=None,
                current_media_normalized=False,
            )


_service: CurrentMediaService | None = None


def get_current_media_service() -> CurrentMediaService:
    global _service
    if _service is None:
        _service = CurrentMediaService()
    return _service


async def resolve_current_media(user_id: int, bot=None) -> "CurrentMedia | None":
    svc = get_current_media_service()
    cm = await svc.get(user_id)
    if cm is None:
        return None
    # Point 11: is_file guard (not just exists())
    if cm.source_path is not None:
        p = Path(cm.source_path)
        if p.exists() and p.is_file():
            return cm
    # Point 10/12: recovery via re-download stubbed; full in next pass
    logger = get_logger(__name__)
    logger.info("resolve_current_media_missing_file", user_id=user_id, path=cm.source_path)
    return None
