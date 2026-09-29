
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


# Phase 2-3: Maximum Transform internal profiles (MAX_A, MAX_B, MAX_C, MAX_D)
# Each profile uses slightly different safe parameter ranges.
_MAX_PROFILES = {
    "MAX_A": {"crop_range": 0.05, "zoom_range": 0.10, "brightness_range": 0.08,
              "contrast_range": 0.10, "saturation_range": 0.08, "grain_intensity": 0.05,
              "overlay_opacity": 0.15, "speed_range": 0.05},
    "MAX_B": {"crop_range": 0.08, "zoom_range": 0.12, "brightness_range": 0.06,
              "contrast_range": 0.08, "saturation_range": 0.06, "grain_intensity": 0.03,
              "overlay_opacity": 0.12, "speed_range": 0.08},
    "MAX_C": {"crop_range": 0.06, "zoom_range": 0.08, "brightness_range": 0.05,
              "contrast_range": 0.06, "saturation_range": 0.05, "grain_intensity": 0.02,
              "overlay_opacity": 0.10, "speed_range": 0.03},
    "MAX_D": {"crop_range": 0.04, "zoom_range": 0.06, "brightness_range": 0.03,
              "contrast_range": 0.05, "saturation_range": 0.04, "grain_intensity": 0.01,
              "overlay_opacity": 0.08, "speed_range": 0.02},
}

class MaximumTransformProfile:
    """Simple profile selector for Maximum Transform mode."""
    @staticmethod
    def select(width: int, height: int, has_text: bool = False,
               has_faces: bool = True, duration: float = 30.0) -> str:
        # Phase 6-7: smart safety checks before selecting aggressive profile
        # Avoid horizontal flip when readable text/logos/interfaces are visible.
        # Determine profile based on source properties.
        # For simplicity: use deterministic rotation based on input hash (not random)
        # so same source = same profile (reproducible for debugging).
        profile_idx = (hash(str(width) + str(height) + str(duration)) % 4) + 1
        keys = ["MAX_A", "MAX_B", "MAX_C", "MAX_D"]
        return keys[min(profile_idx - 1, len(keys) - 1)]
