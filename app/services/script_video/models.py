"""Script video generation schemas.

VideoPlan -> scenes -> hybrid media -> motion -> assembly.
Validated before any expensive AI call.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional, Sequence

from pydantic import BaseModel, Field, field_validator


class SceneSchema(BaseModel):
    id: str = Field(..., description="Scene identifier, e.g. s1")
    narration: str = Field(..., description="Text spoken in this scene")
    visual_prompt: str = Field(..., description="Prompt for AI image generation")
    stock_query: str = Field(default="", max_length=100, description="Short English stock-video search query")
    prefer_stock: bool = Field(default=True, description="Prefer real stock footage when suitable")
    duration_hint: float = Field(default=5.0, ge=1.0, le=30.0)
    importance: int = Field(default=3, ge=1, le=5)
    motion_type: str = Field(default="zoom_in", description="zoom_in | zoom_out | pan_left | pan_right | static")
    transition: str = Field(default="fade", description="fade | slide_left | slide_right | cut")
    reference_scene_id: Optional[str] = Field(default=None, description="For visual consistency reuse")

    @field_validator("id", mode="before")
    @classmethod
    def normalize_id(cls, value: Any) -> str:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if isinstance(value, float) and not value.is_integer():
                return str(value)
            return str(int(value))
        return str(value).strip()

    @field_validator("reference_scene_id", mode="before")
    @classmethod
    def normalize_reference_scene_id(cls, value: Any) -> Optional[str]:
        if value is None or value == "":
            return None
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if isinstance(value, float) and not value.is_integer():
                return str(value)
            return str(int(value))
        return str(value).strip() or None

    @field_validator("stock_query", mode="before")
    @classmethod
    def normalize_stock_query(cls, value: Any) -> str:
        return " ".join(str(value or "").split())[:100]

    @field_validator("duration_hint", mode="before")
    @classmethod
    def normalize_duration_hint(cls, value: Any) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            parsed = 5.0
        return max(1.0, min(30.0, parsed))

    @field_validator("importance", mode="before")
    @classmethod
    def normalize_importance(cls, value: Any) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = 3
        return max(1, min(5, parsed))

    @field_validator("motion_type", mode="before")
    @classmethod
    def normalize_motion(cls, value: Any) -> str:
        """Map free-form camera language onto the renderer motion enum."""
        raw = str(value or "").strip().lower().replace("-", "_")
        compact = " ".join(raw.replace("_", " ").split())
        aliases = {
            "zoom in": "zoom_in",
            "slow zoom in": "zoom_in",
            "slow zoom": "zoom_in",
            "push in": "zoom_in",
            "slow push in": "zoom_in",
            "gentle push in": "zoom_in",
            "dolly in": "zoom_in",
            "move in": "zoom_in",
            "zoom out": "zoom_out",
            "slow zoom out": "zoom_out",
            "pull out": "zoom_out",
            "dolly out": "zoom_out",
            "move out": "zoom_out",
            "pan left": "pan_left",
            "slow pan left": "pan_left",
            "slide left": "pan_left",
            "truck left": "pan_left",
            "pan right": "pan_right",
            "slow pan right": "pan_right",
            "slow pan": "pan_right",
            "slide right": "pan_right",
            "slide": "pan_right",
            "truck right": "pan_right",
            "gentle drift": "zoom_in",
            "drift": "zoom_in",
            "handheld": "zoom_in",
            "tracking": "pan_right",
            "orbit": "pan_right",
            "tilt up": "zoom_in",
            "tilt down": "zoom_out",
            "rack focus": "static",
            "static": "static",
            "static hold": "static",
            "hold": "static",
            "still": "static",
        }
        if compact in aliases:
            return aliases[compact]
        if "left" in compact and any(word in compact for word in ("pan", "slide", "truck", "move")):
            return "pan_left"
        if "right" in compact and any(word in compact for word in ("pan", "slide", "truck", "move")):
            return "pan_right"
        if any(word in compact for word in ("out", "away", "back")) and any(
            word in compact for word in ("zoom", "pull", "dolly", "move")
        ):
            return "zoom_out"
        if any(word in compact for word in ("zoom", "push", "dolly", "closer", "in")):
            return "zoom_in"
        if any(word in compact for word in ("pan", "slide", "truck", "tracking", "orbit")):
            return "pan_right"
        if any(word in compact for word in ("static", "still", "hold", "focus")):
            return "static"
        return "zoom_in"

    @field_validator("motion_type")
    @classmethod
    def validate_motion(cls, v: str) -> str:
        allowed = {"zoom_in", "zoom_out", "pan_left", "pan_right", "static"}
        if v not in allowed:
            raise ValueError(f"motion_type must be one of {allowed}")
        return v

    @field_validator("transition", mode="before")
    @classmethod
    def normalize_transition(cls, value: Any) -> str:
        raw = str(value or "").strip().lower().replace("-", "_")
        compact = " ".join(raw.replace("_", " ").split())
        aliases = {
            "fade": "fade",
            "crossfade": "fade",
            "cross fade": "fade",
            "dissolve": "fade",
            "cut": "cut",
            "hard cut": "cut",
            "jump cut": "cut",
            "slide": "slide_left",
            "slide left": "slide_left",
            "slide right": "slide_right",
            "swipe left": "slide_left",
            "swipe right": "slide_right",
        }
        if compact in aliases:
            return aliases[compact]
        if "right" in compact and any(word in compact for word in ("slide", "swipe")):
            return "slide_right"
        if any(word in compact for word in ("slide", "swipe")):
            return "slide_left"
        if any(word in compact for word in ("fade", "dissolve")):
            return "fade"
        return "cut"

    @field_validator("transition")
    @classmethod
    def validate_transition(cls, v: str) -> str:
        allowed = {"fade", "slide_left", "slide_right", "cut"}
        if v not in allowed:
            raise ValueError(f"transition must be one of {allowed}")
        return v


class VideoPlanSchema(BaseModel):
    title: str = Field(..., min_length=3, max_length=120)
    language: str = Field(default="ru", min_length=2, max_length=5)
    target_duration: int = Field(default=40, ge=15, le=120, description="Target seconds")
    narration: str = Field(..., description="Full narration script")
    visual_style: str = Field(..., min_length=10, max_length=500)
    scenes: Sequence[SceneSchema] = Field(..., min_length=3, max_length=15)

    @field_validator("title", mode="before")
    @classmethod
    def normalize_title(cls, value: Any) -> str:
        title = " ".join(str(value or "").split())[:120]
        return title if len(title) >= 3 else "ReCut video"

    @field_validator("language", mode="before")
    @classmethod
    def normalize_language(cls, value: Any) -> str:
        raw = str(value or "ru").strip().lower().replace("_", "-")
        short = raw.split("-", 1)[0]
        return short if short in {"ru", "en", "de", "fr", "es", "it"} else "ru"

    @field_validator("target_duration", mode="before")
    @classmethod
    def normalize_target_duration(cls, value: Any) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = 40
        return max(15, min(120, parsed))

    @field_validator("visual_style", mode="before")
    @classmethod
    def normalize_visual_style(cls, value: Any) -> str:
        style = " ".join(str(value or "").split())[:500]
        return style if len(style) >= 10 else "Realistic vertical short-form video"

    @field_validator("language")
    @classmethod
    def validate_lang(cls, v: str) -> str:
        allowed = {"ru", "en", "de", "fr", "es", "it"}
        if v not in allowed:
            raise ValueError(f"language must be one of {allowed}")
        return v


@dataclass(frozen=True, slots=True)
class Scene:
    id: str
    narration: str
    visual_prompt: str
    duration_hint: float
    importance: int
    motion_type: str
    transition: str
    reference_scene_id: Optional[str] = None
    stock_query: str = ""
    prefer_stock: bool = True


@dataclass(frozen=True, slots=True)
class VideoPlan:
    title: str
    language: str
    target_duration: int
    narration: str
    visual_style: str
    scenes: tuple[Scene, ...]


def from_schema(schema: VideoPlanSchema) -> VideoPlan:
    scenes = tuple(
        Scene(
            id=s.id,
            narration=s.narration,
            visual_prompt=s.visual_prompt,
            duration_hint=s.duration_hint,
            importance=s.importance,
            motion_type=s.motion_type,
            transition=s.transition,
            reference_scene_id=s.reference_scene_id,
            stock_query=s.stock_query.strip(),
            prefer_stock=s.prefer_stock,
        )
        for s in schema.scenes
    )
    return VideoPlan(
        title=schema.title,
        language=schema.language,
        target_duration=schema.target_duration,
        narration=schema.narration,
        visual_style=schema.visual_style,
        scenes=scenes,
    )


def to_json(plan: VideoPlan) -> str:
    return json.dumps({
        "title": plan.title,
        "language": plan.language,
        "target_duration": plan.target_duration,
        "narration": plan.narration,
        "visual_style": plan.visual_style,
        "scenes": [
            {
                "id": s.id,
                "narration": s.narration,
                "visual_prompt": s.visual_prompt,
                "stock_query": s.stock_query,
                "prefer_stock": s.prefer_stock,
                "duration_hint": s.duration_hint,
                "importance": s.importance,
                "motion_type": s.motion_type,
                "transition": s.transition,
                "reference_scene_id": s.reference_scene_id,
            }
            for s in plan.scenes
        ],
    }, ensure_ascii=False)
