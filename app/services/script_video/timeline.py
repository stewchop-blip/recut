"""Compile a script plan into short, audio-length visual beats."""
from __future__ import annotations

import math
from dataclasses import dataclass

from app.services.script_video.models import VideoPlan


@dataclass(frozen=True, slots=True)
class VisualBeat:
    scene_id: str
    image_index: int
    duration: float
    motion: str
    transition: str


class TimelineCompiler:
    """Compile planned scenes into visual beats without needless visual reuse.

    Quality V3 keeps provider spend bounded, so the editor must squeeze more
    perceived shots out of each paid keyframe. Long still-image scenes are split
    into short editorial beats with alternating crop/motion while stock clips
    keep their native movement. This raises pacing without another API call.
    """

    # Around 3 seconds is a better ceiling for Shorts/Reels than the old 4.2s.
    # A ~4.4s planned scene will now become two distinct editorial beats rather
    # than one long hold, while very short scenes remain a single beat.
    beat_target_seconds = 3.0
    transition_seconds = 0.22

    @staticmethod
    def _second_motion(motion: str) -> str:
        return {
            "zoom_in": "pan_left",
            "zoom_out": "pan_right",
            "pan_left": "zoom_in",
            "pan_right": "zoom_out",
            "static": "zoom_in",
        }.get(motion, "zoom_in")

    def compile(self, plan: VideoPlan, image_count: int, audio_duration: float) -> list[VisualBeat]:
        if image_count < 1:
            raise ValueError("Timeline requires at least one image")
        if not plan.scenes:
            raise ValueError("Timeline requires at least one scene")

        duration = max(1.0, float(audio_duration or plan.target_duration))
        weights = [max(1.0, float(scene.duration_hint)) for scene in plan.scenes]
        weight_total = sum(weights)
        scene_seconds = [duration * weight / weight_total for weight in weights]
        beat_target = self.beat_target_seconds if len(plan.scenes) >= 8 else min(self.beat_target_seconds, 2.8)
        beat_counts = [max(1, math.ceil(seconds / beat_target)) for seconds in scene_seconds]

        # Only xfade/slide transitions shorten the concatenated visual timeline.
        # Internal sub-beats are hard cuts, so compensating for them made the
        # visual stream longer than narration and forced `-shortest` to chop the
        # ending. Add overlap compensation only for actual cross-scene xfade.
        overlaps = sum(
            1
            for scene_index, scene in enumerate(plan.scenes)
            if scene_index > 0 and scene.transition != "cut"
        )
        visual_duration = duration + overlaps * self.transition_seconds
        scene_seconds = [visual_duration * weight / weight_total for weight in weights]

        beats: list[VisualBeat] = []
        for scene_index, (scene, seconds, count) in enumerate(zip(plan.scenes, scene_seconds, beat_counts)):
            image_index = round(scene_index * (image_count - 1) / max(1, len(plan.scenes) - 1))
            beat_duration = seconds / count
            for beat_index in range(count):
                transition = "cut" if not beats else (scene.transition if beat_index == 0 else "cut")
                motion = scene.motion_type if beat_index % 2 == 0 else self._second_motion(scene.motion_type)
                beats.append(VisualBeat(
                    scene_id=scene.id,
                    image_index=image_index,
                    duration=beat_duration,
                    motion=motion,
                    transition=transition,
                ))
        return beats
