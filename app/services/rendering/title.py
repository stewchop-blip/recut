"""TitleRenderer (Phase 7, audit #31-32) — Pillow-based title layer.

Renders the title INSIDE the title_box from TemplateCompositor (never
hardcoded y=h*0.05). Output: transparent PNG with:
  word wrap (max 2 lines), auto font fit (max width 84% box, font 56-70px
  downscale for long text), line spacing, optional rounded plate
  (rgba(0,0,0,0.45), radius 28), soft shadow.
"""
from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path

from app.core.logging import get_logger

logger = get_logger(__name__)

_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/arial.ttf",
]


@dataclass(slots=True, frozen=True)
class TitleStyle:
    variant: str = "minimal"   # minimal | plate | accent
    color: tuple = (255, 255, 255, 255)
    plate_color: tuple = (0, 0, 0, 115)
    shadow: bool = True


@dataclass(slots=True, frozen=True)
class TitleBox:
    x: int
    y: int
    width: int
    height: int


class TitleRenderer:
    def _font(self, size: int):
        from PIL import ImageFont
        for p in _FONT_CANDIDATES:
            if Path(p).exists():
                return ImageFont.truetype(p, size)
        return ImageFont.load_default(size)

    def render(self, text: str, box: TitleBox, *,
               style: TitleStyle | None = None,
               max_lines: int = 2,
               max_width_frac: float = 0.84,
               font_size: int = 64,
               min_font_size: int = 40) -> bytes:
        """Render title into box; returns PNG bytes (transparent)."""
        from PIL import Image, ImageDraw
        style = style or TitleStyle()
        if not text:
            return self._empty_png(box)

        max_w = int(box.width * max_width_frac)
        size = font_size
        lines = self._wrap(text, size, max_w)
        while (len(lines) > max_lines or
               self._width(lines, size) > max_w) and size > min_font_size:
            size -= 4
            lines = self._wrap(text, size, max_w)
        font = self._font(size)
        line_h = size + int(size * 0.32)          # аккуратный line spacing
        block_h = line_h * len(lines)
        pad_x = 34 if style.variant == "plate" else 0
        pad_y = 22 if style.variant == "plate" else 0

        img_w, img_h = box.width, box.height
        img = Image.new("RGBA", (img_w, img_h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        if style.variant == "plate":
            plate_w = min(max_w + 2 * pad_x, box.width)
            plate_x = (box.width - plate_w) // 2
            plate_y = 0
            draw.rounded_rectangle(
                (plate_x, plate_y, plate_x + plate_w, plate_y + block_h + 2 * pad_y),
                radius=28, fill=style.plate_color)

        y0 = (box.height - block_h) // 2 - (pad_y if style.variant == "plate" else 0)
        x_text = (box.width - self._width(lines, size)) // 2
        yy = y0
        if style.shadow:
            from PIL import ImageFilter, ImageChops, Image as PILImage
            sh = PILImage.new("RGBA", (img_w, img_h), (0, 0, 0, 0))
            sd = ImageDraw.Draw(sh)
            for ln in lines:
                sd.text((x_text + 3, yy + 3), ln, font=font, fill=(0, 0, 0, 160))
                yy += line_h
            sh = sh.filter(ImageFilter.GaussianBlur(4))
            img = PILImage.alpha_composite(sh, img)
            draw = ImageDraw.Draw(img)
            yy = y0  # reset after shadow loop
        for ln in lines:
            draw.text((x_text, yy), ln, font=font, fill=style.color)
            yy += line_h
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    # -- helpers ---------------------------------------------------------
    def _wrap(self, text: str, size: int, max_w: int) -> list[str]:
        words = text.split()
        lines: list[str] = []
        cur = ""
        for w in words:
            cand = (cur + " " + w).strip()
            if self._text_w(cand, size) <= max_w or not cur:
                cur = cand
            else:
                lines.append(cur)
                cur = w
        if cur:
            lines.append(cur)
        return lines

    def _text_w(self, s: str, size: int) -> int:
        from PIL import Image
        font = self._font(size)
        return font.getbbox(s)[2] - font.getbbox(s)[0] if s else 0

    def _width(self, lines: list[str], size: int) -> int:
        return max((self._text_w(l, size) for l in lines), default=0)

    @staticmethod
    def _empty_png(box: TitleBox) -> bytes:
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGBA", (box.width, box.height), (0, 0, 0, 0)).save(buf, format="PNG")
        return buf.getvalue()