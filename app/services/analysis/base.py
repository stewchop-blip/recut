"""Abstract clip-selector interface.

The contract is small: give it the transcript + total duration, get
back N clip candidates with validated timestamps. Everything else
(transcript size cap, LLM call details, retry policy) lives in the
implementation.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Sequence


@dataclass(slots=True, frozen=True)
class ClipCandidate:
    """One LLM-suggested moment to cut out."""
    start: float
    end: float
    title: str
    hook: str
    reason: str


@dataclass(slots=True, frozen=True)
class ClipSelectionRequest:
    """Inputs for clip selection."""
    transcript: str                 # full text, may be truncated upstream
    segments: tuple["TranscriptSegment", ...]   # for richer prompting
    total_duration_seconds: float
    target_count: int = 3
    min_seconds: float = 20.0
    max_seconds: float = 60.0


@dataclass(slots=True, frozen=True)
class TranscriptSegment:
    """Minimal segment metadata the LLM sees."""
    start: float
    end: float
    text: str


@dataclass(slots=True, frozen=True)
class ClipSelection:
    """Result of one selection run."""
    clips: tuple[ClipCandidate, ...]
    model: str
    raw_response: str = ""          # for debugging / logging


class ClipSelectorError(RuntimeError):
    """Base class for selection failures."""


class ClipSelector(ABC):
    """Abstract interface — easy to swap with a different LLM provider."""

    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def model_id(self) -> str: ...

    @abstractmethod
    async def select_clips(self, request: ClipSelectionRequest) -> ClipSelection: ...