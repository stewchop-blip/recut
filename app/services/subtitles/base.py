"""Abstract subtitle builder + Phrase dataclass."""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Sequence


@dataclass(slots=True, frozen=True)
class Phrase:
    """One subtitle line — short text + time range.

    `start` / `end` are absolute seconds in the source video.
    """
    start: float
    end: float
    text: str


@dataclass(slots=True, frozen=True)
class SubtitleBuildRequest:
    """Inputs for building subtitle file for one clip."""
    # Source transcript segments (with absolute timestamps).
    segments: Sequence["TranscriptSegment"]
    # Clip boundaries (we only emit phrases inside this window).
    clip_start: float
    clip_end: float
    # Soft cap on words per visible phrase line (config).
    max_words_per_line: int = 4


@dataclass(slots=True, frozen=True)
class TranscriptSegment:
    start: float
    end: float
    text: str


@dataclass(slots=True, frozen=True)
class SubtitleBuildResult:
    """A rendered subtitle file on disk + metadata."""
    phrases: tuple[Phrase, ...]
    output_path: str         # path to the .ass file
    line_count: int


class SubtitleBuilder(ABC):
    """Build an ASS subtitle file."""

    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def build(self, request: SubtitleBuildRequest) -> SubtitleBuildResult: ...