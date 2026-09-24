"""Этап 4: built-in template registry — backgrounds, titles, brand corner.

Assets are NOT shipped as binaries: backgrounds are generated on the fly
(ffmpeg lavfi / PIL), so the repo stays clean and presets stay editable
in code. The registry is intentionally simple — a dict of presets with
IDs the bot shows as buttons and the renderer resolves to ffmpeg args.

Title: text rendered via ffmpeg drawtext (no font files needed — uses
the system font available in the Docker image; falls back gracefully).
Brand corner: small "ReCut" text in the top-left/right corner, optional.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.core.logging import get_logger

logger = get_logger(__name__)


@dataclass(slots=True, frozen=True)
class BackgroundPreset:
    id: str
    label: str          # button text
    # "blur" = blurred copy of source (default pipeline), "color" = solid
    kind: str
    color: str = "black"


@dataclass(slots=True, frozen=True)
class TitlePreset:
    id: str
    label: str
    text: str


BACKGROUNDS: dict[str, BackgroundPreset] = {
    "blur": BackgroundPreset("blur", "🌫 Размытый фон", "blur"),
    "dark": BackgroundPreset("dark", "⬛ Тёмный", "color", "#101014"),
    "light": BackgroundPreset("light", "⬜ Светлый", "color", "#f2f2f5"),
    "accent": BackgroundPreset("accent", "🟪 Акцент", "color", "#1c1030"),
}

TITLES: dict[str, TitlePreset] = {
    "none": TitlePreset("none", "🚫 Без заголовка", ""),
    "look": TitlePreset("look", "👀 Смотри до конца", "Смотри до конца"),
    "moment": TitlePreset("moment", "⚡ Вот это момент", "Вот это момент"),
    "next": TitlePreset("next", "❓ Что дальше?", "Что произошло дальше?"),
    "wow": TitlePreset("wow", "😱 Не ожидал", "Такого я не ожидал"),
}

BRAND_CORNER_DEFAULT = "ReCut"
