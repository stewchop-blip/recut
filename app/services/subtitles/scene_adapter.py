"""Subtitle adapter for AI-generated narration scenes.

Instead of Whisper transcript segments, this takes VideoPlan scenes
(narration + duration_hint + importance) and produces subtitle phrases
aligned to the video timeline. Each scene's narration is split into
short phrases (<= max_words_per_line) distributed across the scene's
duration.
"""
from __future__ import annotations

from typing import Optional

from app.core.config import get_settings
from app.services.subtitles.base import Phrase, SubtitleBuildRequest, SubtitleBuildResult, SubtitleBuilder
from app.services.script_video.models import Scene, VideoPlan


class SceneSubtitleBuilder(SubtitleBuilder):
    """Build ASS subtitles from VideoPlan scenes (not Whisper segments)."""

    @property
    def name(self) -> str:
        return "scene_ass"

    def build_from_plan(
        self,
        plan: VideoPlan,
        start_offset: float = 0.0,
    ) -> SubtitleBuildResult:
        """Generate subtitles from scene narration distributed over timeline."""
        settings = get_settings()
        max_words = max(1, settings.subtitle_max_words_per_line)
        phrases: list[Phrase] = []
        current_time = start_offset

        for scene in plan.scenes:
            scene_duration = max(0.5, scene.duration_hint)
            text = scene.narration.strip()
            if not text:
                current_time += scene_duration
                continue
            words = text.split()
            if not words:
                current_time += scene_duration
                continue
            words_per_chunk = max(1, min(len(words), max_words))
            # If too many words for duration, compress; if few, spread evenly
            chunk_size = max(1, max_words)
            chunk_duration = scene_duration / max(1, (len(words) // chunk_size + (1 if len(words) % chunk_size else 0)))

            for i in range(0, len(words), chunk_size):
                chunk_words = words[i:i + chunk_size]
                chunk_start = current_time + (i / max(1, len(words))) * scene_duration
                chunk_end = min(chunk_start + chunk_duration, current_time + scene_duration)
                if chunk_end <= chunk_start:
                    chunk_end = chunk_start + 0.5
                phrases.append(Phrase(
                    start=chunk_start,
                    end=chunk_end,
                    text=" ".join(chunk_words),
                ))
            current_time += scene_duration

        if not phrases:
            return SubtitleBuildResult(phrases=(), output_path="", line_count=0)

        # Write ASS file using existing ASS format logic (reuse from ass.py if needed)
        import tempfile
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".ass", delete=False, encoding="utf-8"
        ) as fh:
            ass_path = fh.name
            fh.write(self._render_ass(phrases, settings))
        return SubtitleBuildResult(
            phrases=tuple(phrases),
            output_path=ass_path,
            line_count=len(phrases),
        )

    def build(self, request: SubtitleBuildRequest) -> SubtitleBuildResult:
        # This adapter expects VideoPlan, not SubtitleBuildRequest.
        # For compatibility with abstract interface, we raise clearly.
        raise NotImplementedError(
            "SceneSubtitleBuilder uses build_from_plan(VideoPlan) instead of build(SubtitleBuildRequest)"
        )

    @staticmethod
    def _render_ass(phrases: list[Phrase], settings) -> str:
        # Reuse ASS rendering logic from existing subtitle service.
        from app.services.subtitles.ass import AssSubtitleBuilder, _fmt_ts, _escape
        # Replicate layout logic briefly
        width = settings.output_width
        height = settings.output_height
        margin_v = settings.subtitle_safe_bottom_px
        margin_l = settings.subtitle_safe_left_px
        margin_r = settings.subtitle_safe_right_px
        style_line = (
            f"Style: Default,Arial,56,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,"
            f"1,0,0,0,100,100,0,0,1,4,1,2,{margin_l},{margin_r},{margin_v},1"
        )
        header = (
            "[Script Info]\n"
            "ScriptType: v4.00+\n"
            f"PlayResX: {width}\n"
            f"PlayResY: {height}\n"
            "WrapStyle: 0\n"
            "ScaledBorderAndShadow: yes\n"
        )
        events_lines = [
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"
        ]
        for p in phrases:
            events_lines.append(
                f"Dialogue: 0,{_fmt_ts(p.start)},{_fmt_ts(p.end)},Default,,0,0,0,,{_escape(p.text)}"
            )
        style_section = (
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
            "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
            "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
            "Alignment, MarginL, MarginR, MarginV, Encoding\n"
            + style_line + "\n"
        )
        return (
            header
            + "\n[V4+ Styles]\n" + style_section
            + "\n[Events]\n" + "\n".join(events_lines) + "\n"
        )
