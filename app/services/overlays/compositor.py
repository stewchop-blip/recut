"""TemplateCompositor (PHASE C) — box layout for one template render.

A template describes rectangular boxes on the canvas:

  canvas     — full output frame (e.g. 1080x1920)
  video_box  — where the source video sits. The video is ALWAYS fitted
               inside with CONTAIN (fit_inside): never stretched, never
               cropped.
  title_box  — top text band
  overlay_box— banner/CTA area (position resolved by burn_cta)
  decoration_box — decorative insert corner box (PHASE B)
  brand_box  — brand-corner tag area

The compositor computes concrete pixel boxes; the FFmpeg renderer maps
them to scale/overlay expressions. Single source of truth for layout —
no pipeline stage invents its own aspect math (see geometry.py).
"""
from __future__ import annotations

from dataclasses import dataclass

from app.services.media.geometry import fit_inside, GeometryValidationError


@dataclass(slots=True, frozen=True)
class Box:
    x: int
    y: int
    width: int
    height: int


@dataclass(slots=True, frozen=True)
class TemplateSpec:
    """One template's canvas layout. All sizes are pixels."""
    canvas_width: int
    canvas_height: int
    title_height_frac: float = 0.10    # top band for title
    overlay_height_frac: float = 0.15  # bottom band for banner
    side_margin_frac: float = 0.0      # horizontal padding for video box

    @property
    def canvas_box(self) -> Box:
        return Box(0, 0, self.canvas_width, self.canvas_height)

    def video_box(self) -> Box:
        """The area the video may occupy (between title and overlay bands)."""
        top = int(self.canvas_height * self.title_height_frac)
        bottom_band = int(self.canvas_height * self.overlay_height_frac)
        side = int(self.canvas_width * self.side_margin_frac)
        return Box(
            x=side,
            y=top,
            width=self.canvas_width - 2 * side,
            height=self.canvas_height - top - bottom_band,
        )

    def title_box(self) -> Box:
        return Box(0, 0, self.canvas_width,
                   int(self.canvas_height * self.title_height_frac))

    def overlay_box(self) -> Box:
        h = int(self.canvas_height * self.overlay_height_frac)
        return Box(0, self.canvas_height - h, self.canvas_width, h)

    def brand_box(self) -> Box:
        w = int(self.canvas_width * 0.18)
        h = int(self.canvas_height * 0.045)
        return Box(self.canvas_width - w - int(self.canvas_width * 0.03),
                   int(self.canvas_height * 0.02), w, h)

    def decoration_box(self, max_w_frac: float, max_h_frac: float,
                       anchor: str = "top_left") -> Box:
        """Corner box for a decorative insert (PHASE B)."""
        w = int(self.canvas_width * max_w_frac)
        h = int(self.canvas_height * max_h_frac)
        margin_x = int(self.canvas_width * 0.02)
        margin_y = int(self.canvas_height * 0.06)  # below the title band
        left = anchor.endswith("left")
        top = anchor.startswith("top")
        return Box(
            x=margin_x if left else self.canvas_width - w - margin_x,
            y=margin_y if top else self.canvas_height - h - int(self.canvas_height * 0.06),
            width=w, height=h,
        )

    def fit_video(self, source_display_ratio: float) -> Box:
        """CONTAIN the source inside video_box — centered. NEVER stretched."""
        box = self.video_box()
        w, h = fit_inside(source_display_ratio, box.width, box.height)
        # TZ Phase 8: geometry invariant — the fitted box must preserve the
        # source ratio (±1%). Violation = bug, fail loudly, never send.
        fitted_ratio = w / max(h, 1)
        if abs(fitted_ratio - source_display_ratio) >= 0.01:
            raise GeometryValidationError(
                f"compositor invariant violated: fitted {w}x{h} "
                f"ratio={fitted_ratio:.4f} != source ratio="
                f"{source_display_ratio:.4f}")
        return Box(
            x=box.x + (box.width - w) // 2,
            y=box.y + (box.height - h) // 2,
            width=w, height=h,
        )
