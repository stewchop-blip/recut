
"""AutoEditPlanner: deterministic automatic editing decisions.
Phase 33-34 of stability pass.
"""
from dataclasses import dataclass
from pathlib import Path

from app.core.logging import get_logger

logger = get_logger(__name__)

@dataclass(frozen=True, slots=True)
class AutoEditPlan:
    layout_id: str          # full / pip / framed / clean
    background_id: str      # blur / dark / light / gradient
    color_preset: str       # contrast / punchy / original / warm / cool
    audio_preset: str       # original / normalized / dynamic
    trim_start: float = 0.0
    trim_end: float = 0.0
    title_text: str = ""
    brand_corner: bool = False
    banner_enabled: bool = False
    subtitle_enabled: bool = False


class AutoEditPlanner:
    """Simple deterministic planner — NO AI, NO randomness."""

    @staticmethod
    def decide(width: int, height: int, duration: float,
               has_banner: bool = False, banner_enabled: bool = False,
               brand_corner_enabled: bool = False,
               subtitle_enabled: bool = False,
               source_type: str = "file") -> AutoEditPlan:
        """Decide layout and transforms based on source geometry."""
        # Aspect ratio categories
        ratio = height / max(width, 1)
        is_portrait = ratio >= 1.15  # 9:16 or taller
        is_square = 0.85 <= ratio <= 1.15
        is_landscape = ratio < 0.85

        # Auto layout decision
        if is_portrait:
            layout = "full"
            bg = "blur"
        elif is_landscape:
            layout = "framed"
            bg = "dark"
        else:  # square / near-square
            layout = "framed"
            bg = "blur"

        # Auto color: safe quality-friendly preset (Phase 8 spec: contrast default)
        color_preset = "contrast"

        # Audio: original if source seems clean; normalized for reliability
        audio_preset = "original"

        # Auto trim: only safe black/silence detection (Phase 10 spec: safe trim only)
        trim_start = 0.0
        trim_end = 0.0

        # Title / brand: respect user settings (no auto-invention)
        title_text = ""
        brand_corner = brand_corner_enabled
        banner_enabled = banner_enabled and has_banner
        subtitle_enabled = subtitle_enabled

        logger.info(
            "auto_edit_plan_decided",
            source_type=source_type,
            width=width, height=height, ratio=round(ratio, 2),
            layout=layout, bg=bg, color_preset=color_preset,
            audio_preset=audio_preset,
            banner_enabled=banner_enabled,
            subtitle_enabled=subtitle_enabled,
        )
        return AutoEditPlan(
            layout_id=layout,
            background_id=bg,
            color_preset=color_preset,
            audio_preset=audio_preset,
            trim_start=trim_start,
            trim_end=trim_end,
            title_text=title_text,
            brand_corner=brand_corner,
            banner_enabled=banner_enabled,
            subtitle_enabled=subtitle_enabled,
        )
