"""Compile planned story events against the existing narration clock."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Sequence

from app.services.script_video.models import VideoPlan


@dataclass(frozen=True, slots=True)
class VisualBeat:
    scene_id: str
    image_index: int
    duration: float
    motion: str
    transition: str
    importance: int = 3
    framing_scale: float = 1.0


class TimelineCompiler:
    """Use actual scene events, not arbitrary cuts into the same composition.

    An unlocated story object is not permission to crop. The existing planner
    already supplies distinct shots; repeated stills retain one camera path.
    """

    transition_seconds = 0.22

    @staticmethod
    def _scene_durations(plan: VideoPlan, duration: float, phrases: Sequence[object]) -> list[float]:
        weights = [max(1.0, float(scene.duration_hint)) for scene in plan.scenes]
        fallback = [duration * weight / sum(weights) for weight in weights]
        script = [re.findall(r"\w+", scene.narration.casefold()) for scene in plan.scenes]
        # Subtitle text is already the TTS script, even when Whisper supplies
        # timing. Require exact coverage rather than silently align other words.
        tokens: list[str] = []
        intervals: list[tuple[float, float]] = []
        previous_end = 0.0
        for phrase in phrases:
            words = re.findall(r"\w+", str(getattr(phrase, "text", "")).casefold())
            try:
                start, end = float(phrase.start), float(phrase.end)
            except (AttributeError, TypeError, ValueError):
                return fallback
            if not words or not all(map(math.isfinite, (start, end))):
                return fallback
            if start < previous_end - 0.05 or end <= start or start < 0 or end > duration + 0.1:
                return fallback
            step = (end - start) / len(words)
            intervals.extend((start + i * step, start + (i + 1) * step) for i in range(len(words)))
            tokens.extend(words)
            previous_end = end
        if not intervals or tokens != [word for words in script for word in words] or not all(script):
            return fallback
        boundaries = [0.0]
        consumed = 0
        for words in script[:-1]:
            consumed += len(words)
            boundaries.append((intervals[consumed - 1][1] + intervals[consumed][0]) / 2)
        boundaries.append(duration)
        result = [end - start for start, end in zip(boundaries, boundaries[1:])]
        return result if min(result) >= 0.1 else fallback

    def compile(
        self, plan: VideoPlan, image_count: int, audio_duration: float,
        *, phrases: Sequence[object] = (), assets: Sequence[object] = (),
    ) -> list[VisualBeat]:
        if image_count < 1 or not plan.scenes:
            raise ValueError("Timeline requires scenes and at least one visual asset")
        duration = float(audio_duration or plan.target_duration)
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("Timeline requires a finite positive audio duration")
        seconds = self._scene_durations(plan, duration, phrases)
        indices = [round(i * (image_count - 1) / max(1, len(plan.scenes) - 1))
                   for i in range(len(plan.scenes))]
        beats: list[VisualBeat] = []
        for index, (scene, length, image_index) in enumerate(zip(plan.scenes, seconds, indices)):
            same_source = index > 0 and (
                assets[image_index] == assets[indices[index - 1]] if len(assets) == image_count
                else image_index == indices[index - 1]
            )
            # Reveals need a legible cut. Never dissolve a picture into itself.
            short_edge = index > 0 and min(seconds[index - 1], length) < 2 * self.transition_seconds
            transition = (
                "cut" if not beats or same_source or scene.importance >= 4 or short_edge
                else scene.transition
            )
            overlap = self.transition_seconds if transition != "cut" else 0.0
            beats.append(VisualBeat(
                scene.id, image_index, length + overlap, scene.motion_type,
                transition, scene.importance,
            ))
        return beats
