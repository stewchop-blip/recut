"""Memory guard — refuse to start heavy work when free RAM is too low.

We track:
- system-wide `available` memory (psutil.virtual_memory().available)
- after subtracting an estimate for the whisper model we want to load

If the result is below `min_free_mb` we raise MemoryGuardError so the
caller can politely tell the user to retry.
"""
from dataclasses import dataclass

import psutil

from app.core.logging import get_logger

logger = get_logger(__name__)


# Approximate peak RSS for faster-whisper models on CPU (int8).
# These are empirical — bump them up if you see OOM.
WHISPER_PEAK_RSS_MB: dict[str, int] = {
    "tiny": 150,
    "tiny.en": 150,
    "base": 200,
    "base.en": 200,
    "small": 400,
    "small.en": 400,
    "medium": 800,
    "large-v2": 1500,
    "large-v3": 1500,
}


@dataclass(slots=True)
class MemorySnapshot:
    total_mb: int
    available_mb: int
    used_mb: int
    percent: float


class MemoryGuardError(RuntimeError):
    """Not enough free RAM for the requested operation."""


def snapshot() -> MemorySnapshot:
    """Return a fresh memory snapshot (cheap)."""
    vm = psutil.virtual_memory()
    return MemorySnapshot(
        total_mb=vm.total // (1024 * 1024),
        available_mb=vm.available // (1024 * 1024),
        used_mb=vm.used // (1024 * 1024),
        percent=vm.percent,
    )


def estimate_whisper_peak_mb(model: str) -> int:
    """Best-effort RSS estimate for a whisper model on CPU."""
    # Strip any size suffix like 'small.en' → fallback to 'small'.
    for key in WHISPER_PEAK_RSS_MB:
        if model.startswith(key):
            return WHISPER_PEAK_RSS_MB[key]
    return WHISPER_PEAK_RSS_MB["small"]


def require_free_for_whisper(model: str, min_free_mb: int = 150) -> MemorySnapshot:
    """Refuse if available RAM < (whisper_peak + min_free_mb).

    `min_free_mb` is the headroom we want to keep for ffmpeg / python
    / aiohttp / asyncio — roughly 150 MB is enough for typical jobs.
    """
    snap = snapshot()
    peak = estimate_whisper_peak_mb(model)
    needed = peak + min_free_mb
    logger.info(
        "memory_check",
        model=model,
        available_mb=snap.available_mb,
        peak_estimate_mb=peak,
        min_free_mb=min_free_mb,
        needed_mb=needed,
        used_percent=snap.percent,
    )
    if snap.available_mb < needed:
        raise MemoryGuardError(
            f"Not enough free RAM for whisper '{model}': "
            f"have {snap.available_mb} MB, need ~{needed} MB "
            f"(peak {peak} + headroom {min_free_mb}). "
            "Retry in a few minutes."
        )
    return snap