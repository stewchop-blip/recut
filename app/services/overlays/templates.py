"""Template registry — backgrounds, titles, brand corner (Этап 4 + PHASE A).

Assets are NOT shipped as binaries: backgrounds are generated on the fly
(ffmpeg lavfi / PIL), so the repo stays clean and presets stay editable
in code. The registry is intentionally simple — a dict of presets with
IDs the bot shows as buttons and the renderer resolves to ffmpeg args.

Title: text rendered via ffmpeg drawtext (font resolved by MediaService).
Brand corner: small "ReCut" text in the top-right corner, optional.

PHASE A: background library extended with gradients (lavfi `gradients`
source, animated-capable in ffmpeg 7+) and vignette overlays.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.core.logging import get_logger

logger = get_logger(__name__)


@dataclass(slots=True, frozen=True)
class BackgroundPreset:
    id: str
    label: str          # button text
    # "blur"   = blurred copy of source (default pipeline)
    # "color"  = solid color canvas
    # "gradient" = two-color gradient canvas
    # "vignette" = radial darkening over a base color
    kind: str
    color: str = "black"
    color2: str = ""        # gradient second color
    speed: float = 0.0      # gradient rotation speed (0 = static)


@dataclass(slots=True, frozen=True)
class TitlePreset:
    id: str
    label: str
    text: str


BACKGROUNDS: dict[str, BackgroundPreset] = {
    # Classic
    "blur": BackgroundPreset("blur", "🌫 Размытый", "blur"),
    "dark": BackgroundPreset("dark", "⬛ Тёмный", "color", "#101014"),
    "light": BackgroundPreset("light", "⬜ Светлый", "color", "#f2f2f5"),
    "accent": BackgroundPreset("accent", "🟪 Акцент", "color", "#1c1030"),
    # PHASE A — gradients (5-8 curated, generated via lavfi)
    "sunset": BackgroundPreset("sunset", "🌅 Закат", "gradient", "#ff512f", "#dd2476"),
    "ocean": BackgroundPreset("ocean", "🌊 Океан", "gradient", "#2193b0", "#6dd5ed"),
    "night": BackgroundPreset("night", "🌃 Ночь", "gradient", "#0f0c29", "#302b63"),
    "mint": BackgroundPreset("mint", "🌿 Мята", "gradient", "#43cea2", "#185a9d"),
    "candy": BackgroundPreset("candy", "🍬 Карамель", "gradient", "#f953c6", "#b91d73"),
    "ember": BackgroundPreset("ember", "🔥 Уголь", "gradient", "#42275a", "#734b6d"),
}

# Backgrounds whose ffmpeg source is `gradients` (animated-capable).
GRADIENT_IDS = {bg.id for bg in BACKGROUNDS.values() if bg.kind == "gradient"}


@dataclass(slots=True, frozen=True)
class DecorationAsset:
    """PHASE B — one decorative insert / character / arrow / mascot.

    Rendered above the video box, below the banner (z-order), looping
    if animated. NOT shipped in repo; registry entries point at bundled
    asset files added later, or are user-supplied later (PHASE B+).
    """
    id: str
    label: str
    kind: str                 # png | webp | gif | mp4
    path: str                 # bundled asset path ("" = not bundled yet)
    anchor: str = "top_left"  # top_left/top_right/bottom_left/bottom_right
    max_width_frac: float = 0.22   # relative to canvas width
    max_height_frac: float = 0.18  # relative to canvas height


# PHASE B registry — empty for now: no bundled binaries in repo yet.
# Future entries: mascot, reactions, arrows. fill this dict only.
DECORATIONS: dict[str, DecorationAsset] = {}


TITLES: dict[str, TitlePreset] = {
    "none": TitlePreset("none", "🚫 Без заголовка", ""),
    "look": TitlePreset("look", "👀 Смотри до конца", "Смотри до конца"),
    "moment": TitlePreset("moment", "⚡ Вот это момент", "Вот это момент"),
    "next": TitlePreset("next", "❓ Что дальше?", "Что произошло дальше?"),
    "wow": TitlePreset("wow", "😱 Не ожидал", "Такого я не ожидал"),
    # PHASE D: user-defined text — stored in UserSettings.custom_title
    "custom": TitlePreset("custom", "✏️ Свой текст", ""),
}

BRAND_CORNER_DEFAULT = "ReCut"
