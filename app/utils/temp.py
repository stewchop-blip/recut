"""Temporary file management for processing jobs."""

import os
import shutil
import tempfile
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator, BinaryIO, Optional

from app.core.logging import get_logger


logger = get_logger(__name__)


class TempFileManager:
    """Manages temporary files for processing jobs."""

    BASE_DIR = Path("/tmp/recut")

    def __init__(self) -> None:
        self.BASE_DIR.mkdir(parents=True, exist_ok=True)
        # Cleanup stale files on startup
        self.cleanup_stale(max_age_hours=1)

    def _job_dir(self, job_id: str) -> Path:
        """Get job-specific directory."""
        # Validate job_id to prevent path traversal
        safe_id = "".join(c for c in job_id if c.isalnum() or c in "-_")
        if not safe_id:
            safe_id = str(uuid.uuid4())
        return self.BASE_DIR / safe_id

    @asynccontextmanager
    async def job_context(self, job_id: str) -> AsyncGenerator[Path, None]:
        """Context manager for a job's temporary directory."""
        job_dir = self._job_dir(job_id)
        job_dir.mkdir(parents=True, exist_ok=True)
        logger.debug("temp_job_start", job_id=job_id, path=str(job_dir))
        try:
            yield job_dir
        finally:
            self.cleanup_job(job_id)
            logger.debug("temp_job_cleanup", job_id=job_id)

    def save_audio(
        self,
        job_id: str,
        audio_bytes: bytes,
        extension: str = ".mp3",
    ) -> Path:
        """Save audio bytes to job directory."""
        job_dir = self._job_dir(job_id)
        job_dir.mkdir(parents=True, exist_ok=True)

        # Safe filename
        safe_ext = extension if extension.startswith(".") else f".{extension}"
        filename = f"audio{safe_ext}"
        filepath = job_dir / filename

        filepath.write_bytes(audio_bytes)
        logger.debug("temp_audio_saved", job_id=job_id, path=str(filepath), size=len(audio_bytes))
        return filepath

    def save_text(self, job_id: str, text: str, filename: str = "text.txt") -> Path:
        """Save text to job directory."""
        job_dir = self._job_dir(job_id)
        job_dir.mkdir(parents=True, exist_ok=True)

        # Sanitize filename
        safe_name = "".join(c for c in filename if c.isalnum() or c in "._-")
        if not safe_name:
            safe_name = "text.txt"
        filepath = job_dir / safe_name

        filepath.write_text(text, encoding="utf-8")
        return filepath

    def read_file(self, job_id: str, filename: str) -> Optional[bytes]:
        """Read file from job directory."""
        filepath = self._job_dir(job_id) / filename
        if filepath.exists():
            return filepath.read_bytes()
        return None

    def get_audio_path(self, job_id: str, extension: str = ".mp3") -> Optional[Path]:
        """Get path to audio file if exists."""
        safe_ext = extension if extension.startswith(".") else f".{extension}"
        filepath = self._job_dir(job_id) / f"audio{safe_ext}"
        return filepath if filepath.exists() else None

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

    def cleanup_stale(self, max_age_hours: int = 1) -> int:
        """Remove job directories older than max_age_hours."""
        import time
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
        """List active job IDs."""
        if not self.BASE_DIR.exists():
            return []
        return [d.name for d in self.BASE_DIR.iterdir() if d.is_dir()]


# Global instance
temp_manager = TempFileManager()


def get_temp_manager() -> TempFileManager:
    return temp_manager