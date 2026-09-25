"""CleanupService (Phase 9, audit #43) — adapted from reclip-telegram-bot
bot/cleanup.py pattern (reference; structure rewritten for ReCut).

Split TEMP JOB FILES (delete after completion/failure) and CACHE FILES
(TTL + disk limit, oldest first).
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


class CleanupService:
    """Age-based + disk-limit cleanup, oldest files first (audit #43)."""

    def __init__(self) -> None:
        self._settings = get_settings()

    @staticmethod
    def _files_oldest_first(root: Path) -> list[Path]:
        if not root.exists():
            return []
        files = [p for p in root.rglob("*") if p.is_file()]
        files.sort(key=lambda p: p.stat().st_mtime)
        return files

    def cleanup_temp(self, max_age_seconds: int | None = None) -> int:
        """Delete job temp files older than max age (default from settings)."""
        age = max_age_seconds or int(getattr(self._settings, "temp_max_age_seconds", 3600 * 6))
        root = Path(getattr(self._settings, "temp_dir", "/tmp/recut"))
        removed = 0
        now = time.time()
        for p in self._files_oldest_first(root):
            try:
                if now - p.stat().st_mtime > age:
                    p.unlink(missing_ok=True)
                    removed += 1
            except OSError:
                continue
        logger.info("cleanup_temp", removed=removed, root=str(root))
        return removed

    def cleanup_cache(self, max_bytes: int, cache_dir: Path) -> int:
        """Disk-limit cleanup: delete oldest cached files until under limit."""
        removed = 0
        total = 0
        files = self._files_oldest_first(cache_dir)
        sizes = []
        for p in files:
            try:
                sizes.append((p, p.stat().st_size))
            except OSError:
                continue
        total = sum(s for _, s in sizes)
        if total <= max_bytes:
            return 0
        for p, s in sizes:                       # oldest first
            if total <= max_bytes:
                break
            try:
                p.unlink(missing_ok=True)
                total -= s
                removed += 1
            except OSError:
                continue
        logger.info("cleanup_cache", removed=removed, freed=max(0, sum(s for _, s in sizes) - total))
        return removed

    async def run_periodic(self, interval_seconds: int = 1800,
                           cache_max_bytes: int = 2 * 1024 * 1024 * 1024,
                           cache_dir: Path | None = None) -> None:
        """Background loop: temp by age + cache by disk (audit #43)."""
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                self.cleanup_temp()
                if cache_dir is not None:
                    self.cleanup_cache(cache_max_bytes, cache_dir)
            except Exception as e:
                logger.warning("cleanup_periodic_failed", error=str(e)[:150])


_cleanup: CleanupService | None = None


def get_cleanup_service() -> CleanupService:
    global _cleanup
    if _cleanup is None:
        _cleanup = CleanupService()
    return _cleanup