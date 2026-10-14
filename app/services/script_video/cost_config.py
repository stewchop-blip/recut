"""Cost tracking layer for script video generation."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from app.core.config import get_settings


# Model prices (per-token or per-request estimates) fetched from OpenRouter catalog on 2026-10-14.
# Updated manually when changing default models.
PRICE_MAP = {
    # Planner / text
    "anthropic/claude-haiku-5.5": {"input": 1e-7, "output": 5e-7},
    "google/gemini-2.5-flash": {"input": 2e-6, "output": 1e-5},
    # Image (per image output, estimated)
    "google/gemini-nano-banana-2.1": {"image_output": 3e-5},
    "openrouter/auto": {"image_output": 1e-4},
    # TTS (per minute estimated)
    "openai/gpt-audio-mini": {"audio_per_minute": 0.003},
    "google/gemini-3.1-flash-tts-preview": {"audio_per_minute": 0.002},
}


@dataclass(frozen=False)
class CostTracker:
    user_id: int
    job_id: str
    planner_model: str = ""
    image_model: str = ""
    tts_model: str = ""
    images_generated: int = 0
    retries: int = 0
    fallback_used: bool = False
    planner_input_tokens: int = 0
    planner_output_tokens: int = 0
    image_requests: int = 0
    audio_duration_seconds: float = 0.0
    start_time: float = field(default_factory=lambda: __import__("time").time())

    def estimated_cost_usd(self) -> float:
        total = 0.0
        # Planner
        planner_prices = PRICE_MAP.get(self.planner_model, {})
        total += planner_prices.get("input", 0) * self.planner_input_tokens
        total += planner_prices.get("output", 0) * self.planner_output_tokens
        # Images
        image_prices = PRICE_MAP.get(self.image_model, {})
        total += image_prices.get("image_output", 0) * self.images_generated
        # TTS
        tts_prices = PRICE_MAP.get(self.tts_model, {})
        total += tts_prices.get("audio_per_minute", 0) * (self.audio_duration_seconds / 60.0)
        return total


def get_cost_tracker(
    user_id: int,
    job_id: str,
    planner_model: Optional[str] = None,
    image_model: Optional[str] = None,
    tts_model: Optional[str] = None,
) -> CostTracker:
    s = get_settings()
    return CostTracker(
        user_id=user_id,
        job_id=job_id,
        planner_model=planner_model or s.script_video_planner_model,
        image_model=image_model or s.script_video_image_model,
        tts_model=tts_model or s.script_video_tts_model,
    )
