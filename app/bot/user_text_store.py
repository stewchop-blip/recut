"""Tiny in-memory user-text store.

Why not aiogram FSM? MemoryStorage() drops state when the polling loop
restarts or under update contention, which made voice_select fall back
to a placeholder phrase. A plain dict scoped by user_id is simpler and
more predictable for a single-instance polling bot.

Production safety: TTL of 10 minutes, max 10k entries, thread-safe.
"""
import asyncio
import time
from dataclasses import dataclass

_USER_TTL_SECONDS = 600  # 10 min
_MAX_USERS = 10_000


@dataclass
class _PendingText:
    text: str
    prompt_message_id: int
    created_at: float


class UserTextStore:
    def __init__(self) -> None:
        self._data: dict[int, _PendingText] = {}
        self._lock = asyncio.Lock()

    async def put(self, user_id: int, text: str, prompt_message_id: int) -> None:
        async with self._lock:
            self._evict_if_needed()
            self._data[user_id] = _PendingText(
                text=text,
                prompt_message_id=prompt_message_id,
                created_at=time.time(),
            )

    async def get(self, user_id: int) -> _PendingText | None:
        async with self._lock:
            entry = self._data.get(user_id)
            if entry is None:
                return None
            if time.time() - entry.created_at > _USER_TTL_SECONDS:
                self._data.pop(user_id, None)
                return None
            return entry

    async def clear(self, user_id: int) -> None:
        async with self._lock:
            self._data.pop(user_id, None)

    def _evict_if_needed(self) -> None:
        # Drop expired entries
        now = time.time()
        expired = [uid for uid, e in self._data.items() if now - e.created_at > _USER_TTL_SECONDS]
        for uid in expired:
            self._data.pop(uid, None)
        # Drop oldest if still over cap
        if len(self._data) >= _MAX_USERS:
            oldest = sorted(self._data.items(), key=lambda kv: kv[1].created_at)
            for uid, _ in oldest[: len(self._data) - _MAX_USERS + 1]:
                self._data.pop(uid, None)


_store: UserTextStore | None = None


def get_user_text_store() -> UserTextStore:
    global _store
    if _store is None:
        _store = UserTextStore()
    return _store
