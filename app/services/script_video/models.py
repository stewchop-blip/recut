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
        """LLMs sometimes emit numeric ids although the schema wants strings."""
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

    @field_validator("motion_type", mode="before")
    @classmethod
    def normalize_motion(cls, value: Any) -> str:
        """Map harmless natural-language motion labels onto renderer enums.

        This avoids paying for a second planner call only because a model wrote
        `slow push in` instead of the renderer token `zoom_in`.
        """
        raw = str(value or "").strip().lower().replace("-", "_")
        compact = " ".join(raw.replace("_", " ").split())
        aliases = {
            "zoom in": "zoom_in",
            "slow zoom in": "zoom_in",
            "slow zoom": "zoom_in",
            "push in": "zoom_in",
            "slow push in": "zoom_in",
            "gentle push in": "zoom_in",
            "zoom out": "zoom_out",
            "slow zoom out": "zoom_out",
            "pull out": "zoom_out",
            "pan left": "pan_left",
            "slow pan left": "pan_left",
            "pan right": "pan_right",
            "slow pan right": "pan_right",
            "slow pan": "pan_right",
            "gentle drift": "zoom_in",
            "static": "static",
            "static hold": "static",
            "hold": "static",
            "still": "static",
        }
        if compact in aliases:
            return aliases[compact]
        normalized = compact.replace(" ", "_")
        return normalized

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
            "slide": "slide_left",
            "slide left": "slide_left",
            "slide right": "slide_right",
        }
        if compact in aliases:
            return aliases[compact]
        return compact.replace(" ", "_")

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
