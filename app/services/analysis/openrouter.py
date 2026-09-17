"""OpenRouter-based clip selector.

Uses /api/v1/chat/completions with `response_format: {type: json_object}`
to force strict JSON. The model sees ONLY text + timestamps — never the
video. The system prompt encodes all selection criteria from the spec.

Validation:
- JSON must contain a top-level `clips` array
- Each clip's start/end must fall inside [0, total_duration]
- start < end, duration within [min, max]
- clips must not overlap excessively
"""
import asyncio
import json
import re
from typing import Any, Optional

import httpx

from app.core.config import get_settings
from app.core.logging import get_logger
from app.services.analysis.base import (
    ClipCandidate,
    ClipSelector,
    ClipSelectorError,
    ClipSelection,
    ClipSelectionRequest,
    TranscriptSegment,
)

logger = get_logger(__name__)


# System prompt — encodes every requirement from the spec.
SYSTEM_PROMPT = """You are a video editor assistant. Your job is to pick
self-contained moments from a Russian transcript that will work as
standalone short-form vertical videos (TikTok / Instagram Reels /
YouTube Shorts).

SELECTION CRITERIA (each clip must satisfy ALL):
1. The fragment is understandable WITHOUT prior context.
2. The opening hooks the viewer in the first 2-3 seconds.
3. The fragment contains a complete thought (no mid-thought cuts).
4. Useful / surprising / conflicting / story / opinion / fact / conclusion.
5. Minimum filler — avoid long intros, greetings, repetitions.
6. Good ending — do NOT cut mid-sentence.
7. Suitable for vertical short-form.

FORBIDDEN OPENINGS (re-cut if removing them makes the clip stronger):
"ну", "короче", "в общем", "как я говорил ранее", "итак", "собственно",
"кстати", "да", "нет", "вот", "это самое".

PREFERRED LENGTH: 20-60 seconds per clip. Shorter is OK if the moment is
truly standalone.

OUTPUT FORMAT (strict — return ONLY this JSON, no commentary):
{
  "clips": [
    {
      "start": 82.4,
      "end": 117.8,
      "title": "...",
      "hook": "...",
      "reason": "..."
    }
  ]
}

Fields:
- "start" / "end": seconds from the beginning of the source video.
- "title": short title for the clip (max 60 chars, Russian).
- "hook": first-sentence hook the editor should use (max 80 chars).
- "reason": 1-sentence justification for selection (max 120 chars).

Return EXACTLY the requested number of clips unless the transcript is
too short to support that many.
"""


class OpenRouterClipSelector(ClipSelector):
    """LLM via OpenRouter chat/completions with structured JSON."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        timeout_seconds: int | None = None,
        max_retries: int = 1,
    ) -> None:
        s = get_settings()
        self._api_key = api_key or s.openrouter_api_key
        self._model = model or s.clip_analysis_model
        self._timeout = timeout_seconds or s.openrouter_timeout_seconds
        self._max_retries = max_retries
        self._client: httpx.AsyncClient | None = None

    @property
    def name(self) -> str:
        return "openrouter"

    @property
    def model_id(self) -> str:
        return self._model

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url="https://openrouter.ai/api/v1",
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://recut.bot",
                    "X-Title": "Recut Bot",
                },
                timeout=httpx.Timeout(self._timeout, connect=10.0),
            )
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    def _build_user_payload(self, request: ClipSelectionRequest) -> str:
        """Compress transcript into the user prompt."""
        # Build a compact representation: [start-ends] text
        compact_lines: list[str] = []
        for seg in request.segments:
            compact_lines.append(
                f"[{_fmt_time(seg.start)}-{_fmt_time(seg.end)}] {seg.text.strip()}"
            )
        transcript_block = "\n".join(compact_lines)

        return (
            f"TOTAL_VIDEO_DURATION_SECONDS: {request.total_duration_seconds:.1f}\n"
            f"REQUESTED_CLIP_COUNT: {request.target_count}\n"
            f"CLIP_LENGTH_SECONDS: {request.min_seconds:.0f}-{request.max_seconds:.0f}\n\n"
            f"TRANSCRIPT (timestamps in [HH:MM:SS]):\n{transcript_block}"
        )

    async def select_clips(self, request: ClipSelectionRequest) -> ClipSelection:
        client = await self._get_client()
        user_payload = self._build_user_payload(request)

        body = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_payload},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.4,
            "max_tokens": 1500,
        }

        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                logger.info(
                    "openrouter_clip_request",
                    model=self._model,
                    attempt=attempt,
                    transcript_chars=len(user_payload),
                )
                resp = await client.post("/chat/completions", json=body)
            except httpx.TimeoutException as e:
                last_error = e
                logger.warning("openrouter_clip_timeout", attempt=attempt, error=str(e))
                continue
            except httpx.RequestError as e:
                last_error = e
                logger.warning("openrouter_clip_request_error", attempt=attempt, error=str(e))
                continue

            if resp.status_code != 200:
                err = resp.text[:300]
                logger.warning(
                    "openrouter_clip_http_error",
                    attempt=attempt,
                    status=resp.status_code,
                    error=err,
                )
                last_error = ClipSelectorError(f"OpenRouter HTTP {resp.status_code}: {err}")
                continue

            data = resp.json()
            raw = (
                data.get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
            )
            if not raw:
                last_error = ClipSelectorError("Empty content from model")
                continue

            try:
                clips = self._parse_and_validate(raw, request)
            except ClipSelectorError as e:
                last_error = e
                logger.warning(
                    "openrouter_clip_parse_failed",
                    attempt=attempt,
                    error=str(e),
                    raw_first_200=raw[:200],
                )
                continue

            # Successful parse — return.
            logger.info(
                "openrouter_clip_success",
                model=self._model,
                clips=len(clips),
            )
            return ClipSelection(
                clips=tuple(clips),
                model=self._model,
                raw_response=raw[:1000],
            )

        # Out of retries.
        raise ClipSelectorError(
            f"OpenRouter clip selection failed after {self._max_retries + 1} attempts: {last_error}"
        )

    # ---------------------------------------------------------------------------
    # Validation
    # ---------------------------------------------------------------------------

    def _parse_and_validate(
        self, raw: str, request: ClipSelectionRequest,
    ) -> list[ClipCandidate]:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ClipSelectorError(f"Invalid JSON: {e}") from e

        if not isinstance(data, dict):
            raise ClipSelectorError(f"Expected object, got {type(data).__name__}")

        clips_raw = data.get("clips")
        if not isinstance(clips_raw, list):
            raise ClipSelectorError(f"Expected 'clips' array, got {type(clips_raw).__name__}")

        out: list[ClipCandidate] = []
        for i, item in enumerate(clips_raw):
            if not isinstance(item, dict):
                raise ClipSelectorError(f"clip[{i}] is not an object")

            try:
                start = float(item["start"])
                end = float(item["end"])
            except (KeyError, ValueError, TypeError) as e:
                raise ClipSelectorError(f"clip[{i}] bad timestamps: {e}") from e

            title = str(item.get("title", "")).strip()[:200]
            hook = str(item.get("hook", "")).strip()[:200]
            reason = str(item.get("reason", "")).strip()[:200]

            # Range / duration validation.
            if start < 0:
                raise ClipSelectorError(f"clip[{i}] start < 0 ({start})")
            if end > request.total_duration_seconds + 0.5:
                raise ClipSelectorError(
                    f"clip[{i}] end {end} > video duration {request.total_duration_seconds:.1f}"
                )
            if end <= start:
                raise ClipSelectorError(f"clip[{i}] end <= start ({start} → {end})")
            duration = end - start
            # Soft warning — accept slightly outside but flag.
            if duration > request.max_seconds * 1.5 or duration < request.min_seconds * 0.4:
                raise ClipSelectorError(
                    f"clip[{i}] duration {duration:.1f}s outside reasonable bounds "
                    f"[{request.min_seconds:.0f}, {request.max_seconds:.0f}]"
                )

            out.append(ClipCandidate(
                start=start, end=end, title=title, hook=hook, reason=reason,
            ))

        if not out:
            raise ClipSelectorError("LLM returned zero clips")

        # Reject excessive overlap.
        out.sort(key=lambda c: c.start)
        for i in range(len(out) - 1):
            gap = out[i + 1].start - out[i].end
            if gap < -request.min_seconds * 0.3:  # overlap > 30% of min length
                raise ClipSelectorError(
                    f"clips[{i}] and [{i+1}] overlap too much (gap={gap:.1f}s)"
                )

        return out


def _fmt_time(seconds: float) -> str:
    """Format seconds as HH:MM:SS for prompt readability."""
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


# Global instance (re-instantiated lazily so settings changes apply)
_selector: OpenRouterClipSelector | None = None


def get_clip_selector() -> OpenRouterClipSelector:
    global _selector
    if _selector is None:
        _selector = OpenRouterClipSelector()
    return _selector