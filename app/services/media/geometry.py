"""VideoGeometry — single source of truth for aspect/geometry decisions.

Every pipeline stage (QuickPrep, Smart Clips, three_versions, template
renderer) MUST use these helpers instead of inventing its own aspect
logic. All decisions are made on EFFECTIVE DISPLAY GEOMETRY (coded
dimensions × SAR, rotation applied), never on coded dims alone.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.services.media.probe import VideoProbeResult


@dataclass(slots=True, frozen=True)
class DisplayGeometry:
    """Effective display geometry of one file."""
    coded_width: int
    coded_height: int
    display_width: int     # coded × SAR (rotation NOT applied)
    display_height: int
    rotation: int
    effective_width: int   # display, rotation applied
    effective_height: int
    sar: float
    dar: float


def get_display_geometry(meta) -> DisplayGeometry:
    """Build DisplayGeometry from a VideoProbeResult."""
    return DisplayGeometry(
        coded_width=meta.coded_width,
        coded_height=meta.coded_height,
        display_width=meta.effective_width,
        display_height=meta.effective_height,
        rotation=meta.rotation,
        effective_width=meta.effective_width,
        effective_height=meta.effective_height,
        sar=meta.sample_aspect_ratio,
        dar=meta.display_aspect_ratio,
    )


def is_near_aspect(meta, ratio: float, tolerance: float = 0.05) -> bool:
    """True if the file's EFFECTIVE DISPLAY ratio is within tolerance.

    ratio: target as w/h (e.g. 9/16 for vertical).
    """
    eff_w = meta.effective_width
    eff_h = meta.effective_height
    if eff_w <= 0 or eff_h <= 0:
        return False
    return abs(eff_w / eff_h - ratio) < tolerance


def fit_inside(
    source_display_ratio: float,
    box_width: int,
    box_height: int,
) -> tuple[int, int]:
    """CONTAIN: largest (w, h) with the source display ratio inside the box.

    Even dims (H.264 requires /2). Never stretched, never cropped.
    """
    if box_width <= 0 or box_height <= 0 or source_display_ratio <= 0:
        return (box_width, box_height)
    w = box_width
    h = int(round(w / source_display_ratio))
    if h > box_height:
        h = box_height
        w = int(round(h * source_display_ratio))
    w = max(2, w - w % 2)
    h = max(2, h - h % 2)
    return (w, h)
