"""Rate limits and quota enforcement."""

import time
from dataclasses import dataclass
from typing import Literal

from app.core.config import get_settings


@dataclass(slots=True)
class LimitCheckResult:
    """Result of a limit check."""
    allowed: bool
    reason: str | None = None
    retry_after_seconds: int | None = None
    current_count: int | None = None
    limit: int | None = None


class LimitExceeded(Exception):
    """Raised when a limit is exceeded."""

    def __init__(self, result: LimitCheckResult):
        self.result = result
        super().__init__(result.reason or "Limit exceeded")


class LimitsManager:
    """Manages rate limits and quotas."""

    def __init__(self) -> None:
        self.settings = get_settings()
        # In-memory storage for MVP (replace with Redis later if needed)
        # Structure: {user_id: {date_str: count}}
        self._generation_counts: dict[int, dict[str, int]] = {}
        # Idempotency keys: {key: timestamp}
        self._idempotency_keys: dict[str, float] = {}
        # TTL for idempotency keys (1 hour)
        self._idempotency_ttl = 3600

    def _today_str(self) -> str:
        return time.strftime("%Y-%m-%d", time.localtime())

    def check_text_length(self, text: str) -> LimitCheckResult:
        """Validate text length."""
        length = len(text)
        limit = self.settings.max_text_length
        if length > limit:
            return LimitCheckResult(
                allowed=False,
                reason=f"Текст слишком длинный: {length} символов (максимум {limit})",
                current_count=length,
                limit=limit,
            )
        return LimitCheckResult(allowed=True, current_count=length, limit=limit)

    def check_daily_generations(self, user_id: int) -> LimitCheckResult:
        """Check daily generation quota."""
        today = self._today_str()
        user_counts = self._generation_counts.get(user_id, {})
        current = user_counts.get(today, 0)
        limit = self.settings.max_generations_per_day

        if current >= limit:
            # Calculate seconds until midnight
            import datetime
            now = datetime.datetime.now()
            tomorrow = (now + datetime.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            retry_after = int((tomorrow - now).total_seconds())
            return LimitCheckResult(
                allowed=False,
                reason=f"Дневной лимит исчерпан ({limit} генераций). Попробуйте завтра.",
                retry_after_seconds=retry_after,
                current_count=current,
                limit=limit,
            )
        return LimitCheckResult(allowed=True, current_count=current, limit=limit)

    def increment_generation(self, user_id: int) -> int:
        """Increment generation counter for user."""
        today = self._today_str()
        if user_id not in self._generation_counts:
            self._generation_counts[user_id] = {}
        self._generation_counts[user_id][today] = self._generation_counts[user_id].get(today, 0) + 1
        return self._generation_counts[user_id][today]

    def check_idempotency(self, key: str) -> LimitCheckResult:
        """Check if request with this key was already processed."""
        now = time.time()
        # Clean expired keys
        self._clean_expired_keys(now)

        if key in self._idempotency_keys:
            return LimitCheckResult(
                allowed=False,
                reason="Запрос уже обрабатывается (двойной клик)",
                retry_after_seconds=1,
            )
        return LimitCheckResult(allowed=True)

    def mark_idempotency(self, key: str) -> None:
        """Mark key as processed."""
        self._idempotency_keys[key] = time.time()

    def _clean_expired_keys(self, now: float) -> None:
        expired = [k for k, ts in self._idempotency_keys.items() if now - ts > self._idempotency_ttl]
        for k in expired:
            del self._idempotency_keys[k]

    def get_stats(self, user_id: int) -> dict:
        """Get current usage stats for user."""
        today = self._today_str()
        current = self._generation_counts.get(user_id, {}).get(today, 0)
        return {
            "generations_today": current,
            "limit": self.settings.max_generations_per_day,
            "remaining": max(0, self.settings.max_generations_per_day - current),
        }


# Global instance
limits_manager = LimitsManager()


def get_limits_manager() -> LimitsManager:
    return limits_manager