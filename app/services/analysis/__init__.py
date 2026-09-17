"""LLM-based analysis services.

Stage E uses the transcript from Stage D to find interesting moments
that can stand alone as TikTok / Reels / Shorts.

The LLM gets ONLY text + timestamps, never the video itself.
"""
from app.services.analysis.base import (
    ClipCandidate,
    ClipSelection,
    ClipSelectionRequest,
    ClipSelector,
    ClipSelectorError,
)

__all__ = [
    "ClipCandidate",
    "ClipSelection",
    "ClipSelectionRequest",
    "ClipSelector",
    "ClipSelectorError",
]