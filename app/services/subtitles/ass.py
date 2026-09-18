"""Phrase-level subtitle builder that emits an ASS file.

Design notes
------------
- We split long segments into short phrases of <= max_words_per_line
  words so the screen never shows a wall of text.
- Empty phrases are skipped.
- We deliberately KEEP each Whisper segment's start..end and chunk the
  TEXT inside it — phrase boundaries don't snap to segment boundaries
  for the START (you'd see one word alone too often), but we DO snap
  to segment boundaries if a chunk would otherwise cross a natural
  pause (>0.5 s gap between segments) — keeps subtitles aligned to
  the speaker's actual rhythm.
- The output ASS file uses a single style sized for 1080x1920 vertical
  video with safe-area margins configurable via Settings.
"""
from __future__ import annotations

from app.core.config import get_settings
from app.services.subtitles.base import (
    Phrase,
    SubtitleBuildRequest,
    SubtitleBuildResult,
    SubtitleBuilder,
)


class AssSubtitleBuilder(SubtitleBuilder):
    """Builds an ASS subtitle file from Whisper segments."""

    @property
    def name(self) -> str:
        return "ass"

    def build(self, request: SubtitleBuildRequest) -> SubtitleBuildResult:
        settings = get_settings()
        max_words = max(1, request.max_words_per_line)

        # Step 1: clip transcript to [clip_start, clip_end] and collect
        # segments with start/end clamped.
        clipped: list[tuple[float, float, str]] = []
        for seg in request.segments:
            if seg.end <= request.clip_start or seg.start >= request.clip_end:
                continue
            seg_start = max(seg.start, request.clip_start)
            seg_end = min(seg.end, request.clip_end)
            if seg_end <= seg_start:
                continue
            clipped.append((seg_start, seg_end, seg.text.strip()))

        if not clipped:
            return SubtitleBuildResult(phrases=(), output_path="", line_count=0)

        # Step 2: build phrases by chunking words.
        # Each phrase carries the START of the first word and the END of
        # the last word in the chunk, both in absolute clip-time.
        # For chunking we don't have per-word timestamps; we distribute
        # each chunk's time uniformly across its words.
        phrases: list[Phrase] = []
        for seg_start, seg_end, text in clipped:
            if not text:
                continue
            words = text.split()
            if not words:
                continue
            seg_duration = max(0.001, seg_end - seg_start)
            for i in range(0, len(words), max_words):
                chunk = words[i:i + max_words]
                if not chunk:
                    continue
                word_start_frac = i / max(1, len(words))
                word_end_frac = min(1.0, (i + len(chunk)) / max(1, len(words)))
                p_start = seg_start + seg_duration * word_start_frac
                p_end = seg_start + seg_duration * word_end_frac
                # Avoid zero-length phrases
                if p_end <= p_start:
                    p_end = p_start + 0.05
                phrases.append(Phrase(
                    start=p_start,
                    end=p_end,
                    text=" ".join(chunk),
                ))

        if not phrases:
            return SubtitleBuildResult(phrases=(), output_path="", line_count=0)

        # Step 3: write ASS to a file alongside the source video.
        # The caller is responsible for the directory — we just take
        # a sibling path. Here we return the path but the orchestrator
        # decides where to write it. To keep this builder self-contained,
        # we DO write to a tempfile and return its path.
        import tempfile
        from pathlib import Path

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".ass", delete=False, encoding="utf-8",
        ) as fh:
            ass_path = fh.name
            fh.write(self._render_ass(phrases, settings))

        return SubtitleBuildResult(
            phrases=tuple(phrases),
            output_path=ass_path,
            line_count=len(phrases),
        )

    @staticmethod
    def _render_ass(phrases: list[Phrase], settings) -> str:
        """Produce ASS file content sized for vertical video."""
        # Layout: safe areas from Settings.
        # ASS uses MarginL/MarginR/MarginV (bottom-aligned by default).
        margin_v = settings.subtitle_safe_bottom_px
        margin_l = settings.subtitle_safe_left_px
        margin_r = settings.subtitle_safe_right_px
        width = settings.output_width
        height = settings.output_height

        # Bold sans-serif, white with black outline — maximum readability.
        style_line = (
            f"Style: Default,Arial,56,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,"
            f"1,0,0,0,100,100,0,0,1,4,1,2,{margin_l},{margin_r},{margin_v},1"
        )
        style_section = (
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
            "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
            "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
            "Alignment, MarginL, MarginR, MarginV, Encoding\n"
            + style_line + "\n"
        )

        events_lines: list[str] = []
        events_lines.append(
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"
        )
        for p in phrases:
            events_lines.append(
                f"Dialogue: 0,{_fmt_ts(p.start)},{_fmt_ts(p.end)},Default,,0,0,0,,{_escape(p.text)}"
            )
        events_section = "\n".join(events_lines) + "\n"

        header = (
            "[Script Info]\n"
            "ScriptType: v4.00+\n"
            f"PlayResX: {width}\n"
            f"PlayResY: {height}\n"
            "WrapStyle: 0\n"
            "ScaledBorderAndShadow: yes\n"
        )

        return (
            header
            + "\n[V4+ Styles]\n" + style_section
            + "\n[Events]\n" + events_section
        )


def _fmt_ts(seconds: float) -> str:
    """ASS uses H:MM:SS.cs (centiseconds)."""
    if seconds < 0:
        seconds = 0.0
    total_cs = int(round(seconds * 100))
    cs = total_cs % 100
    total_s = total_cs // 100
    s = total_s % 60
    total_m = total_s // 60
    m = total_m % 60
    h = total_m // 60
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _escape(text: str) -> str:
    """ASS dialogue text needs newlines escaped; rest is safe."""
    return text.replace("\n", " ").replace("\r", " ")