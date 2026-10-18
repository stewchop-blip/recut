"""Subtitle adapter for AI-generated narration scenes."""
from __future__ import annotations

import tempfile
import re
from difflib import SequenceMatcher
from types import SimpleNamespace
from typing import Optional, Sequence

from app.core.config import get_settings
from app.services.script_video.models import VideoPlan
from app.services.subtitles.base import Phrase, SubtitleBuildRequest, SubtitleBuildResult, SubtitleBuilder


class SceneSubtitleBuilder(SubtitleBuilder):
    """Build ASS subtitles from plan timing or real word timestamps."""

    @property
    def name(self) -> str:
        return "scene_ass"

    def build_from_plan(
        self,
        plan: VideoPlan,
        start_offset: float = 0.0,
        total_duration: Optional[float] = None,
    ) -> SubtitleBuildResult:
        settings = get_settings()
        max_words = max(1, settings.subtitle_max_words_per_line)
        phrases: list[Phrase] = []
        current_time = start_offset
        weights = [max(0.1, float(scene.duration_hint)) for scene in plan.scenes]
        weight_total = sum(weights) or 1.0
        timeline_duration = total_duration or sum(weights)

        for scene, weight in zip(plan.scenes, weights):
            scene_duration = max(0.1, timeline_duration * weight / weight_total)
            words = scene.narration.strip().split()
            if not words:
                current_time += scene_duration
                continue
            chunk_count = max(1, (len(words) + max_words - 1) // max_words)
            chunk_duration = scene_duration / chunk_count
            for i in range(0, len(words), max_words):
                chunk = words[i:i + max_words]
                chunk_index = i // max_words
                start = current_time + chunk_index * chunk_duration
                end = min(start + chunk_duration, current_time + scene_duration)
                phrases.append(Phrase(start=start, end=max(end, start + 0.05), text=" ".join(chunk)))
            current_time += scene_duration

        return self._write_phrases(phrases, settings, karaoke=True)

    def build_from_words(
        self,
        words: Sequence[object],
        *,
        start_offset: float = 0.0,
    ) -> SubtitleBuildResult:
        """Build karaoke subtitles from real STT word timestamps."""
        settings = get_settings()
        max_words = max(1, settings.subtitle_max_words_per_line)
        clean: list[tuple[float, float, str]] = []
        for word in words:
            text = str(getattr(word, "text", "") or "").strip()
            if not text:
                continue
            try:
                start = float(getattr(word, "start")) + start_offset
                end = float(getattr(word, "end")) + start_offset
            except (TypeError, ValueError):
                continue
            if end <= start:
                end = start + 0.05
            clean.append((max(0.0, start), max(0.05, end), text))

        if not clean:
            return SubtitleBuildResult(phrases=(), output_path="", line_count=0)

        groups: list[list[tuple[float, float, str]]] = []
        current: list[tuple[float, float, str]] = []
        for item in clean:
            current.append(item)
            terminal = item[2].rstrip().endswith((".", "!", "?", "…", ",", ":", ";"))
            if len(current) >= max_words or (terminal and len(current) >= 2):
                groups.append(current)
                current = []
        if current:
            groups.append(current)

        phrases = [
            Phrase(start=g[0][0], end=max(g[-1][1], g[0][0] + 0.08), text=" ".join(x[2] for x in g))
            for g in groups
        ]
        ass = self._render_word_ass(groups, settings)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".ass", delete=False, encoding="utf-8") as fh:
            fh.write(ass)
            path = fh.name
        return SubtitleBuildResult(phrases=tuple(phrases), output_path=path, line_count=len(phrases))

    def build_from_script_and_words(
        self,
        script: str,
        recognized_words: Sequence[object],
    ) -> SubtitleBuildResult:
        """Keep the *exact TTS script* on screen, using STT only for timing.

        Whisper is an imperfect transcriber even when speech was synthesized
        from a known text. Using its guessed text creates spelling mistakes and
        invented words. SequenceMatcher anchors recognizable words to their
        audio timestamps; occasional mismatches are mapped locally.
        """
        source = script.split()
        raw = []
        for item in recognized_words:
            word = str(getattr(item, "text", "") or "").strip()
            if not word:
                continue
            try:
                start = float(getattr(item, "start"))
                end = float(getattr(item, "end"))
            except (ValueError, TypeError):
                continue
            if start < 0 or end <= start:
                continue
            raw.append((start, end, word))

        if not source or not raw:
            return SubtitleBuildResult(phrases=(), output_path="", line_count=0)

        def normalized(word: str) -> str:
            return re.sub(r"[^\w]", "", word.casefold().replace("ё", "е"))

        clean_source = [normalized(word) for word in source]
        clean_recognized = [normalized(word) for _, _, word in raw]
        matcher = SequenceMatcher(None, clean_source, clean_recognized, autojunk=False)
        operations = matcher.get_opcodes()
        matched = sum(i2 - i1 for tag, i1, i2, _, _ in operations if tag == "equal")
        # If the transcription doesn't broadly resemble the script (wrong
        # language, hallucinations, etc), use the safe plan-based fallback.
        if matched < max(2, int(len(source) * 0.55)):
            return SubtitleBuildResult(phrases=(), output_path="", line_count=0)

        times: list[tuple[float, float] | None] = [None] * len(source)
        for tag, i1, i2, j1, j2 in operations:
            if tag == "equal":
                for offset in range(i2 - i1):
                    times[i1 + offset] = raw[j1 + offset][:2]
            elif tag == "replace" and j2 > j1:
                count = i2 - i1
                recognized_count = j2 - j1
                if count == recognized_count:
                    for offset in range(count):
                        times[i1 + offset] = raw[j1 + offset][:2]
                else:
                    # A phrase can be split/merged differently by ASR. Spread
                    # the true script words over the corresponding audio span.
                    start, end = raw[j1][0], raw[j2 - 1][1]
                    for offset in range(count):
                        st = start + (end - start) * offset / count
                        en = start + (end - start) * (offset + 1) / count
                        times[i1 + offset] = (st, en)

        index = 0
        while index < len(times):
            if times[index] is not None:
                index += 1
                continue
            end_index = index
            while end_index < len(times) and times[end_index] is None:
                end_index += 1
            count = end_index - index
            before = times[index - 1][1] if index > 0 and times[index - 1] else None
            after = times[end_index][0] if end_index < len(times) and times[end_index] else None
            if before is None:
                after_value = after if after is not None else raw[0][0]
                left = max(0.0, after_value - 0.24 * count)
                right = max(left + 0.05 * count, after_value)
            else:
                left = before
                right = after if after is not None and after > before else before + 0.24 * count
            for offset in range(count):
                st = left + (right - left) * offset / count
                en = left + (right - left) * (offset + 1) / count
                times[index + offset] = (st, en)
            index = end_index

        aligned: list[SimpleNamespace] = []
        prev_start = 0.0
        for word, entry in zip(source, times):
            assert entry is not None
            start, end = entry
            start = max(0.0, prev_start, start)
            end = max(start + 0.04, end)
            aligned.append(SimpleNamespace(text=word, start=start, end=end))
            prev_start = start

        return self.build_from_words(aligned)

    def build(self, request: SubtitleBuildRequest) -> SubtitleBuildResult:
        raise NotImplementedError(
            "SceneSubtitleBuilder uses build_from_plan(VideoPlan) or build_from_words(words)"
        )

    @staticmethod
    def _style(settings) -> tuple[str, str]:
        width = settings.output_width
        height = settings.output_height
        margin_v = settings.subtitle_safe_bottom_px
        margin_l = settings.subtitle_safe_left_px
        margin_r = settings.subtitle_safe_right_px
        style = (
            f"Style: Default,Arial,56,&H00FFFFFF,&H0000D7FF,&H00000000,&H80000000,"
            f"1,0,0,0,100,100,0,0,1,4,1,2,{margin_l},{margin_r},{margin_v},1"
        )
        header = (
            "[Script Info]\nScriptType: v4.00+\n"
            f"PlayResX: {width}\nPlayResY: {height}\nWrapStyle: 0\nScaledBorderAndShadow: yes\n"
            "\n[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
            "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
            "Alignment, MarginL, MarginR, MarginV, Encoding\n"
            + style + "\n\n[Events]\n"
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        )
        return header, style

    def _write_phrases(self, phrases: list[Phrase], settings, *, karaoke: bool) -> SubtitleBuildResult:
        if not phrases:
            return SubtitleBuildResult(phrases=(), output_path="", line_count=0)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".ass", delete=False, encoding="utf-8") as fh:
            fh.write(self._render_ass(phrases, settings, karaoke=karaoke))
            path = fh.name
        return SubtitleBuildResult(phrases=tuple(phrases), output_path=path, line_count=len(phrases))

    @staticmethod
    def _render_ass(phrases: list[Phrase], settings, *, karaoke: bool = True) -> str:
        from app.services.subtitles.ass import _escape, _fmt_ts
        header, _ = SceneSubtitleBuilder._style(settings)
        lines: list[str] = []
        for p in phrases:
            words = p.text.split()
            if not words:
                continue
            if karaoke:
                total_cs = max(1, int(round((p.end - p.start) * 100)))
                base, remainder = divmod(total_cs, len(words))
                text = "".join(
                    f"{{\\k{base + (1 if i < remainder else 0)}}}{_escape(word)} "
                    for i, word in enumerate(words)
                ).rstrip()
            else:
                text = _escape(p.text)
            lines.append(
                f"Dialogue: 0,{_fmt_ts(p.start)},{_fmt_ts(p.end)},Default,,0,0,0,,{text}"
            )
        return header + "\n".join(lines) + "\n"

    @staticmethod
    def _render_word_ass(groups: list[list[tuple[float, float, str]]], settings) -> str:
        from app.services.subtitles.ass import _escape, _fmt_ts
        header, _ = SceneSubtitleBuilder._style(settings)
        lines: list[str] = []
        for group in groups:
            start = group[0][0]
            end = max(group[-1][1], start + 0.08)
            pieces: list[str] = []
            for i, (word_start, word_end, text) in enumerate(group):
                next_start = group[i + 1][0] if i + 1 < len(group) else word_end
                duration = max(0.01, max(word_end, next_start) - word_start)
                pieces.append(f"{{\\k{max(1, int(round(duration * 100)))}}}{_escape(text)} ")
            lines.append(
                f"Dialogue: 0,{_fmt_ts(start)},{_fmt_ts(end)},Default,,0,0,0,,{''.join(pieces).rstrip()}"
            )
        return header + "\n".join(lines) + "\n"
