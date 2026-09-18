"""CTA overlay service.

Resolves CTA positioning + timing into a concrete FFmpeg overlay
expression. Stage I.

Modes:
- off:    no overlay
- full:   overlay the whole clip
- start:  first N seconds
- end:    last N seconds (most common for our case)
- range:  explicit [start_seconds, end_seconds] window

Positions (string):
- top / bottom — full-width centred on the edge
- top_left / top_right / bottom_left / bottom_right — corner

Safe areas are enforced via Settings.subtitle_safe_*_px.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


@dataclass(slots=True, frozen=True)
class CTAOverlaySpec:
    """Computed placement for one CTA on one clip."""
    asset_path: Path
    x: int                   # pixel offset from left
    y: int                   # pixel offset from top
    start_seconds: float
    end_seconds: float

    @property
    def has_window(self) -> bool:
        return self.end_seconds > self.start_seconds


class CTAService:
    """Builds CTAOverlaySpec from settings + clip duration."""

    def __init__(self) -> None:
        s = get_settings()
        self._enabled = s.cta_enabled
        self._mode = s.cta_mode
        self._position = s.cta_position
        self._start_seconds = s.cta_start_seconds
        self._duration_seconds = s.cta_duration_seconds
        self._margin = s.cta_min_margin_px
        self._w = s.output_width
        self._h = s.output_height

    def resolve(
        self,
        clip_duration: float,
        configured_asset: str,
        fallback_dir: Path,
    ) -> CTAOverlaySpec | None:
        """Return None if CTA is disabled."""
        if not self._enabled:
            return None

        from app.services.overlays.cta_generator import ensure_cta_asset
        asset, _ = ensure_cta_asset(configured_asset, fallback_dir)

        # Determine time window.
        if self._mode == "full":
            t0, t1 = 0.0, clip_duration
        elif self._mode == "start":
            t0, t1 = 0.0, min(self._duration_seconds, clip_duration)
        elif self._mode == "end":
            t0 = max(0.0, clip_duration - self._duration_seconds)
            t1 = clip_duration
        elif self._mode == "range":
            t0 = self._start_seconds
            t1 = self._start_seconds + self._duration_seconds
        else:
            logger.warning("cta_unknown_mode", mode=self._mode)
            return None
        if t1 <= t0:
            return None

        # Determine position. x/y are the top-left of the CTA box.
        # CTA box assumed ~720x200 by default; FFmpeg scales it via overlay
        # expressions using iw/ih of the asset. We choose TOP-LEFT corner.
        if self._position == "top":
            x = (self._w - 720) // 2
            y = self._margin
        elif self._position == "bottom":
            x = (self._w - 720) // 2
            y = self._h - 200 - self._margin
        elif self._position == "top_left":
            x = self._margin
            y = self._margin
        elif self._position == "top_right":
            x = self._w - 720 - self._margin
            y = self._margin
        elif self._position == "bottom_left":
            x = self._margin
            y = self._h - 200 - self._margin
        elif self._position == "bottom_right":
            x = self._w - 720 - self._margin
            y = self._h - 200 - self._margin
        else:
            logger.warning("cta_unknown_position", position=self._position)
            return None

        return CTAOverlaySpec(
            asset_path=asset,
            x=x, y=y,
            start_seconds=t0,
            end_seconds=t1,
        )

    def _make_spec(
        self,
        *,
        clip_duration: float,
        mode: str,
        duration_seconds: float,
        start_seconds: float,
        position: str,
        margin: int,
        output_w: int,
        output_h: int,
        asset: Path,
    ) -> CTAOverlaySpec | None:
        """Build a CTAOverlaySpec from explicit params (used by Quick Prep).

        Same logic as `resolve()` but doesn't pull from Settings — the
        caller passes every value. Useful when settings come from
        per-user DB rows instead of env vars.
        """
        if clip_duration <= 0:
            return None

        # Time window
        if mode == "full":
            t0, t1 = 0.0, clip_duration
        elif mode == "start":
            t0, t1 = 0.0, min(duration_seconds, clip_duration)
        elif mode == "end":
            t0 = max(0.0, clip_duration - duration_seconds)
            t1 = clip_duration
        elif mode == "range":
            t0 = start_seconds
            t1 = start_seconds + duration_seconds
        else:
            logger.warning("cta_unknown_mode", mode=mode)
            return None
        if t1 <= t0:
            return None

        # Position (assume 720x200 CTA box)
        if position == "top":
            x = (output_w - 720) // 2
            y = margin
        elif position == "bottom":
            x = (output_w - 720) // 2
            y = output_h - 200 - margin
        elif position == "top_left":
            x = margin
            y = margin
        elif position == "top_right":
            x = output_w - 720 - margin
            y = margin
        elif position == "bottom_left":
            x = margin
            y = output_h - 200 - margin
        elif position == "bottom_right":
            x = output_w - 720 - margin
            y = output_h - 200 - margin
        else:
            logger.warning("cta_unknown_position", position=position)
            return None

        return CTAOverlaySpec(
            asset_path=asset,
            x=x, y=y,
            start_seconds=t0,
            end_seconds=t1,
        )