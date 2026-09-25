"""TransformationPreset (PART 23-24) — the single user-facing settings object.

One preset = one consistent editorial package (background + title +
brand + video layout + audio + banner defaults). Callers pass a preset,
not a scattered list of parameters.

Four built-in presets (PART 24):
    clean  -> blur bg, no title, no brand, small banner, original audio
    meme   -> dark bg, meme title, large banner, dynamic audio
    brand  -> accent bg, brand title, brand corner ON, medium banner
    custom -> whatever the user's DB settings say (style_pick / fine_menu)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class TransformationPreset:
    """One coherent user-facing preset (PART 23-24)."""
    name: str           # internal id: clean / meme / brand / custom / ...
    label: str          # button text shown to user
    # Layout / visuals
    background_id: str
    title_id: str
    brand_corner: bool
    cta_size: str        # small / medium / large
    # Content / media
    audio_preset: str    # original / dynamic / music / none
    speed: float         # playback speed (1.0 = original)
    color_preset: str = "original"  # TZ Phase 15: original/contrast/warm/cool/punchy


# PART 24 — user-facing presets (shown as one-tap buttons in Style picker)
BUILTIN_PRESETS = {
    "clean": TransformationPreset(
        "clean", "⚪️ Чистый",
        background_id="blur", title_id="none", brand_corner=False,
        cta_size="small", audio_preset="original", speed=1.0,
    ),
    "meme": TransformationPreset(
        "meme", "😎 Мем",
        background_id="dark", title_id="wow", brand_corner=False,
        cta_size="large", audio_preset="dynamic", speed=1.0,
    ),
    "brand": TransformationPreset(
        "brand", "🏷 Бренд",
        background_id="accent", title_id="none", brand_corner=True,
        cta_size="medium", audio_preset="original", speed=1.0,
    ),
}

PRESET_KEYS = list(BUILTIN_PRESETS.keys())


def resolve_preset(preset_id: str | None) -> TransformationPreset | None:
    """Resolve preset id -> TransformationPreset; 'custom' -> None."""
    p = BUILTIN_PRESETS.get(preset_id) if preset_id else None
    return p


# TZ Phase 15 — color presets (eq filter params, no manual sliders).
@dataclass(slots=True, frozen=True)
class ColorPreset:
    name: str
    label: str
    brightness: float = 0.0
    contrast: float = 1.0
    saturation: float = 1.0
    gamma: float = 1.0


COLOR_PRESETS: dict[str, ColorPreset] = {
    "original": ColorPreset("original", "Оригинал"),
    "contrast": ColorPreset("contrast", "Контраст", contrast=1.12, gamma=0.97),
    "warm": ColorPreset("warm", "Тёплый", contrast=1.05, saturation=1.1, gamma=1.03),
    "cool": ColorPreset("cool", "Холодный", contrast=1.04, saturation=1.05, gamma=0.96),
    "punchy": ColorPreset("punchy", "Сочный", contrast=1.18, saturation=1.25, brightness=0.02),
}


def eq_filter(color_name: str) -> str:
    """FFmpeg eq filter string, '' when original."""
    p = COLOR_PRESETS.get(color_name)
    if p is None:
        return ""
    parts = ["eq=1"]
    if p.brightness:
        parts.append(f"brightness={p.brightness}")
    if p.contrast != 1.0:
        parts.append(f"contrast={p.contrast}")
    if p.saturation != 1.0:
        parts.append(f"saturation={p.saturation}")
    if p.gamma != 1.0:
        parts.append(f"gamma={p.gamma}")
    return ":".join(parts).replace("eq=1:", "eq=")
