"""Temporary file management for processing jobs.

`BASE_DIR` comes from `settings.temp_dir` (default `/tmp/recut`) so we
can override it on Railway by mounting a volume if needed.

Path-traversal hardening: every job_id is sanitized through
`_safe_id()` before being used in a filesystem path.
"""
import shutil
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator, Optional

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


class TempFileManager:
    """Manages temporary directories for processing jobs."""

    def __init__(self) -> None:
        self.BASE_DIR = Path(get_settings().temp_dir)
        self.BASE_DIR.mkdir(parents=True, exist_ok=True)
        # Cleanup stale files on startup (settings-driven TTL)
        self.cleanup_stale(max_age_hours=2)

    @staticmethod
    def _safe_id(job_id: str) -> str:
        """Sanitize a job_id to a safe filesystem component."""
        cleaned = "".join(c for c in job_id if c.isalnum() or c in "-_")
        return cleaned or uuid.uuid4().hex

    def _job_dir(self, job_id: str) -> Path:
        return self.BASE_DIR / self._safe_id(job_id)

    @asynccontextmanager
    async def job_context(self, job_id: str) -> AsyncGenerator[Path, None]:
        """Context manager: yield a fresh job_dir, clean it up on exit."""
        job_dir = self._job_dir(job_id)
        job_dir.mkdir(parents=True, exist_ok=True)
        logger.debug("temp_job_start", job_id=job_id, path=str(job_dir))
        try:
            yield job_dir
        finally:
            self.cleanup_job(job_id)
            logger.debug("temp_job_cleanup", job_id=job_id)

    def cleanup_job(self, job_id: str) -> bool:
        """Remove job directory and all contents."""
        job_dir = self._job_dir(job_id)
        if job_dir.exists():
            try:
                shutil.rmtree(job_dir)
                logger.debug("temp_job_removed", job_id=job_id)
                return True
            except OSError as e:
                logger.warning("temp_job_cleanup_failed", job_id=job_id, error=str(e))
                return False
        return False

    def cleanup_stale(self, max_age_hours: float = 2.0) -> int:
        """Remove job directories older than max_age_hours."""
        now = time.time()
        max_age_seconds = max_age_hours * 3600
        removed = 0

        if not self.BASE_DIR.exists():
            return 0

        for item in self.BASE_DIR.iterdir():
            if not item.is_dir():
                continue
            try:
                mtime = item.stat().st_mtime
                if now - mtime > max_age_seconds:
                    shutil.rmtree(item)
                    removed += 1
                    logger.info("temp_stale_removed", path=str(item))
            except OSError as e:
                logger.warning("temp_stale_cleanup_failed", path=str(item), error=str(e))

        if removed:
            logger.info("temp_stale_cleanup_done", removed=removed)
        return removed

    def list_jobs(self) -> list[str]:
        """List active job directory names."""
        if not self.BASE_DIR.exists():
            return []
        return [d.name for d in self.BASE_DIR.iterdir() if d.is_dir()]


# Global instance (re-instantiated lazily so tests can override Settings)
_temp: Optional[TempFileManager] = None


def get_temp_manager() -> TempFileManager:
    """Lazy singleton — recreated if Settings.temp_dir has changed."""
    global _temp
    expected = Path(get_settings().temp_dir).resolve()
    if _temp is None or _temp.BASE_DIR.resolve() != expected:
        _temp = TempFileManager()
    return _temp
