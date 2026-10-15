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
    """Split each narrated scene into subtle motion beats, reusing its still."""

    beat_target_seconds = 2.8
    transition_seconds = 0.18

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
        beat_counts = [max(1, math.ceil(seconds / self.beat_target_seconds)) for seconds in scene_seconds]

        transitions = 0
        for scene_index, (scene, count) in enumerate(zip(plan.scenes, beat_counts)):
            transitions += max(0, count - 1)
            if scene_index > 0 and scene.transition != "cut":
                transitions += 1
        visual_duration = duration + transitions * self.transition_seconds
        scene_seconds = [visual_duration * weight / weight_total for weight in weights]

        beats: list[VisualBeat] = []
        for scene_index, (scene, seconds, count) in enumerate(zip(plan.scenes, scene_seconds, beat_counts)):
            image_index = round(scene_index * (image_count - 1) / max(1, len(plan.scenes) - 1))
            beat_duration = seconds / count
            for beat_index in range(count):
                transition = "cut" if not beats else (scene.transition if beat_index == 0 else "fade")
                motion = scene.motion_type if beat_index % 2 == 0 else self._second_motion(scene.motion_type)
                beats.append(VisualBeat(
                    scene_id=scene.id,
                    image_index=image_index,
                    duration=beat_duration,
                    motion=motion,
                    transition=transition,
                ))
        return beats
