"""Cost tracking layer for script video generation."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from app.core.config import get_settings


# Kept as a compatibility export for older imports. OpenRouter prices vary by
# model and output shape, so estimates here must not be used as actual spend.
PRICE_MAP: dict[str, dict[str, float]] = {}


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
    actual_costs: dict[str, float] = field(default_factory=dict)
    start_time: float = field(default_factory=lambda: __import__("time").time())

    def add_actual_cost(self, component: str, cost_usd: float | None) -> None:
        if cost_usd is None or cost_usd < 0:
            return
        self.actual_costs[component] = self.actual_costs.get(component, 0.0) + float(cost_usd)

    @property
    def actual_cost_usd(self) -> float:
        return sum(self.actual_costs.values())

    def estimated_cost_usd(self) -> float:
        """Legacy method name; returns only provider-reported actual spend."""
        return self.actual_cost_usd


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
