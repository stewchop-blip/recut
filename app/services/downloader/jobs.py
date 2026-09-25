"""MediaJobQueue — adapted from reels-downloader-bot download_jobs.py (Apache-2.0).

Global semaphore + per-user lock + inflight URL dedup with asyncio.shield
(one cancelled caller must not kill the shared download). READY media is
NOT a heavy active job (audit #8-10).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Awaitable

from app.core.logging import get_logger

logger = get_logger(__name__)

MAX_CONCURRENT_HEAVY_JOBS = 1
MAX_QUEUED_JOBS = 8


@dataclass(frozen=True, slots=True)
class DownloadResult:
    success: bool
    file_path: Path | None = None
    source_url: str | None = None
    platform: str = ""
    title: str = ""
    duration: float = 0.0
    width: int = 0
    height: int = 0
    error_code: str = ""
    from_cache: bool = False


class MediaJobQueue:
    """One heavy job at a time; 1 per user; inflight URL dedup."""

    def __init__(self, max_heavy: int = MAX_CONCURRENT_HEAVY_JOBS) -> None:
        self._sem = asyncio.Semaphore(max_heavy)
        self._user_locks: dict[int, asyncio.Lock] = {}
        self._inflight: dict[str, asyncio.Task] = {}   # url -> task
        self._active: set[int] = set()                  # users with heavy job

    def _lock_for(self, user_id: int) -> asyncio.Lock:
        if user_id not in self._user_locks:
            self._user_locks[user_id] = asyncio.Lock()
        return self._user_locks[user_id]

    def is_busy(self, user_id: int) -> bool:
        """True when the user has a heavy (active) job — audit #12/16."""
        return user_id in self._active

    async def run_download(self, user_id: int, url: str,
                           work: Callable[[], Awaitable[DownloadResult]]) -> DownloadResult:
        """Run download under global semaphore + per-user lock.

        Duplicate URL while inflight: join the SAME shielded task instead of
        launching a second yt-dlp (audit #10).
        """
        existing = self._inflight.get(url)
        if existing is not None and not existing.done():
            logger.info("download_dedup_join", url=url, user=user_id)
            return await asyncio.shield(existing)

        task = asyncio.create_task(self._run_one(user_id, work))
        self._inflight[url] = task
        try:
            return await asyncio.shield(task)
        finally:
            self._inflight.pop(url, None)

    async def _run_one(self, user_id: int,
                       work: Callable[[], Awaitable[DownloadResult]]) -> DownloadResult:
        if len(self._inflight) > MAX_QUEUED_JOBS:
            return DownloadResult(success=False, error_code="QUEUE_FULL")
        lock = self._lock_for(user_id)
        if lock.locked() or self.is_busy(user_id):
            return DownloadResult(success=False, error_code="QUEUE_FULL")
        self._active.add(user_id)
        try:
            async with self._sem:
                return await work()
        finally:
            self._active.discard(user_id)


_JOB_QUEUE: MediaJobQueue | None = None


def get_job_queue() -> MediaJobQueue:
    global _JOB_QUEUE
    if _JOB_QUEUE is None:
        _JOB_QUEUE = MediaJobQueue()
    return _JOB_QUEUE