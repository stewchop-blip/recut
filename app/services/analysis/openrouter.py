"""OpenRouter-based clip selector.

Uses /api/v1/chat/completions with `response_format: {type: json_object}`
to force strict JSON. The model sees ONLY text + timestamps — never the
video. The system prompt encodes all selection criteria from the spec.

Token optimization:
- Compact transcript format: [MM:SS-MM:SS] text (vs full HH:MM:SS)
- Truncate user payload at 3000 chars (last segments are most relevant)
- Log prompt_tokens / completion_tokens from response.usage

Validation:
- JSON must contain a top-level `clips` array
- Each clip's start/end must fall inside [0, total_duration]
- start < end, duration within [min, max]
- clips must not overlap excessively
"""
import asyncio
import json
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


# System prompt — kept tight to save input tokens.
# Gemini 2.5 Flash charges ~$0.30 / 1M input tokens; this prompt is ~500 tokens.
SYSTEM_PROMPT = """Pick N standalone short-form vertical clips from a Russian transcript.

CRITERIA (each clip must satisfy ALL):
1. Understandable without prior context.
2. Hooks viewer in first 2-3s.
3. Complete thought, no mid-sentence cut.
4. Useful / surprising / story / opinion / fact.
5. Minimal filler, good ending.
6. Length: 20-60s. Shorter OK if truly standalone.

FORBIDDEN OPENINGS: ну, короче, в общем, как я говорил ранее, итак,
собственно, кстати, это самое. Re-cut if removing them strengthens the clip.

OUTPUT (strict JSON, no commentary):
{"clips":[{"start":82.4,"end":117.8,"title":"≤60 chars","hook":"≤80 chars","reason":"≤120 chars"}]}

"start"/"end": seconds from video start. Return EXACTLY N clips unless transcript is too short."""


# Cap on transcript characters sent to the LLM. Russian text averages
# ~2.5 chars/token; 3000 chars ≈ 1200 tokens input. Real-world Russian
# videos of 10 min produce ~4500 chars of transcript; truncating to
# 3000 keeps the most engaging final segments (where videos usually
# climax) and reduces input cost by ~33% per call.
MAX_TRANSCRIPT_CHARS = 3000


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

    @staticmethod
    def _fmt_time(seconds: float) -> str:
        """Format seconds as MM:SS (compact) — saves ~11 chars per segment
        vs HH:MM:SS. For clips < 1h the hour prefix is always 00 and is
        pure overhead.
        """
        seconds = max(0, int(seconds))
        m, s = divmod(seconds, 60)
        return f"{m:02d}:{s:02d}"

    def _build_user_payload(self, request: ClipSelectionRequest) -> str:
        """Build compact user prompt and truncate to MAX_TRANSCRIPT_CHARS.

        Segments are kept in chronological order; we keep the LAST
        MAX_TRANSCRIPT_CHARS characters (the climactic end of videos
        usually has the most shareable moments).
        """
        # Compact [MM:SS-MM:SS] text
        compact_lines: list[str] = []
        for seg in request.segments:
            line = f"[{self._fmt_time(seg.start)}-{self._fmt_time(seg.end)}] {seg.text.strip()}"
            compact_lines.append(line)

        transcript_block = "\n".join(compact_lines)

        header = (
            f"DUR:{request.total_duration_seconds:.0f}s "
            f"COUNT:{request.target_count} "
            f"LEN:{request.min_seconds:.0f}-{request.max_seconds:.0f}s\n"
        )

        # If transcript is under the cap, send it whole.
        if len(transcript_block) <= MAX_TRANSCRIPT_CHARS:
            return header + transcript_block

        # Truncate to the LAST MAX_TRANSCRIPT_CHARS chars (preserves the
        # most engaging segments which usually come at the end of a video).
        truncated = transcript_block[-MAX_TRANSCRIPT_CHARS:]
        # Align to the start of a segment line for clean formatting.
        nl = truncated.find("\n")
        if nl > 0:
            truncated = truncated[nl + 1:]

        logger.info(
            "transcript_truncated",
            original_chars=len(transcript_block),
            kept_chars=len(truncated),
        )
        return header + "[truncated, last segments only]\n" + truncated

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
                    payload_chars=len(user_payload),
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

            # Log token usage from OpenRouter's response.usage block.
            # Format: {prompt_tokens, completion_tokens, total_tokens}
            usage = data.get("usage") or {}
            if usage:
                logger.info(
                    "openrouter_token_usage",
                    model=self._model,
                    prompt_tokens=usage.get("prompt_tokens", 0),
                    completion_tokens=usage.get("completion_tokens", 0),
                    total_tokens=usage.get("total_tokens", 0),
                )

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
                # If LLM returned zero clips, don't bother retrying —
                # fall back to taking the full video as one clip.
                if "zero clips" in str(e).lower():
                    break
                continue

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

        if last_error is not None and "zero clips" in str(last_error).lower():
            fallback = ClipCandidate(
                start=0.0,
                end=request.total_duration_seconds,
                title="Full video",
                hook="",
                reason="Fallback: LLM returned no clips, using full video",
            )
            logger.info("clip_selector_fallback", total_seconds=request.total_duration_seconds)
            return ClipSelection(
                clips=(fallback,),
                model=self._model,
                raw_response=raw[:1000],
            )

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

            if start < 0:
                raise ClipSelectorError(f"clip[{i}] start < 0 ({start})")
            if end > request.total_duration_seconds + 0.5:
                raise ClipSelectorError(
                    f"clip[{i}] end {end} > video duration {request.total_duration_seconds:.1f}"
                )
            if end <= start:
                raise ClipSelectorError(f"clip[{i}] end <= start ({start} → {end})")
            duration = end - start
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

        out.sort(key=lambda c: c.start)
        for i in range(len(out) - 1):
            gap = out[i + 1].start - out[i].end
            if gap < -request.min_seconds * 0.3:
                raise ClipSelectorError(
                    f"clips[{i}] and [{i+1}] overlap too much (gap={gap:.1f}s)"
                )

        return out


# Global instance (re-instantiated lazily so settings changes apply)
_selector: OpenRouterClipSelector | None = None


def get_clip_selector() -> OpenRouterClipSelector:
    global _selector
    if _selector is None:
        _selector = OpenRouterClipSelector()
    return _selector