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
            if s is None:
                return None
            # Item 9: return CurrentMedia if ANY recoverable identity exists —
            # local_path, telegram_file_id, or source_url. local_path may be None.
            path = Path(s.current_media_path) if s.current_media_path else None
            file_id = getattr(s, "current_media_telegram_file_id", None) or None
            url = getattr(s, "current_media_url", None) or None
            if path is None and file_id is None and url is None:
                return None
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
    """Full recovery (item 8): local file → telegram_file_id → source_url."""
    from app.services.media.probe import get_probe_service
    svc = get_current_media_service()
    cm = await svc.get(user_id)
    if cm is None:
        return None

    # 1) Local file present and is a real file?
    if cm.source_path is not None:
        p = Path(cm.source_path)
        if p.exists() and p.is_file():
            return cm

    current_dir = Path(f"/tmp/recut/current/{user_id}")
    current_dir.mkdir(parents=True, exist_ok=True)

    # 2) Recover from telegram_file_id.
    if cm.telegram_file_id and bot is not None:
        try:
            tg_file = await bot.get_file(cm.telegram_file_id)
            dest = current_dir / f"source{Path(tg_file.file_path).suffix or '.mp4'}"
            await bot.download_file(tg_file.file_path, destination=dest)
            probe = get_probe_service()
            meta = await probe.probe(dest)
            cm = await svc.set_ready(
                user_id, dest, job_id=cm.job_id,
                telegram_file_id=cm.telegram_file_id,
                source_url=cm.source_url,
            )
            logger = get_logger(__name__)
            logger.info("current_media_recovered_telegram",
                        user_id=user_id, path=str(dest),
                        duration=meta.duration_seconds)
            return cm
        except Exception as e:
            logger = get_logger(__name__)
            logger.warning("current_media_telegram_recovery_failed",
                           user_id=user_id, error=str(e)[:200])

    # 3) Recover from source_url (item 10: DownloaderService owns download).
    if cm.source_url:
        try:
            from app.pipeline.url_downloader import DownloaderService
            dl_dir = current_dir / "url"
            dl_dir.mkdir(parents=True, exist_ok=True)
            result = await DownloaderService().download(cm.source_url, dl_dir)
            src = Path(result.path)
            if not src.is_file():
                raise ValueError(f"download result is not a file: {src}")
            dest = current_dir / f"source{src.suffix or '.mp4'}"
            import shutil
            shutil.move(str(src), str(dest))
            probe = get_probe_service()
            meta = await probe.probe(dest)
            cm = await svc.set_ready(
                user_id, dest, job_id=cm.job_id,
                telegram_file_id=cm.telegram_file_id,
                source_url=cm.source_url,
            )
            logger = get_logger(__name__)
            logger.info("current_media_recovered_url",
                        user_id=user_id, path=str(dest),
                        duration=meta.duration_seconds)
            return cm
        except Exception as e:
            logger = get_logger(__name__)
            logger.warning("current_media_url_recovery_failed",
                           user_id=user_id, error=str(e)[:200])

    logger = get_logger(__name__)
    logger.info("current_media_unrecoverable", user_id=user_id)
    return None
