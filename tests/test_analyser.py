"""Tests for Stage E (LLM clip selection).

We mock the OpenRouter HTTP client so no network calls happen.
The validator logic (timestamp ranges, duration, overlap) is the
primary thing under test here.
"""
import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.config import Settings
from app.services.analysis.base import (
    ClipCandidate,
    ClipSelector,
    ClipSelectorError,
    ClipSelection,
    ClipSelectionRequest,
    TranscriptSegment,
)
from app.services.analysis.openrouter import OpenRouterClipSelector
from app.pipeline.analyser import Analyser, AnalyserError
from app.pipeline.transcriber import TranscribedJob
from app.services.transcription.base import Segment


def _settings(**overrides) -> Settings:
    base = dict(
        telegram_bot_token="t",
        openrouter_api_key="fake-key",
        database_url="postgresql+asyncpg://x",
    )
    base.update(overrides)
    return Settings(**base)


def _make_request(total: float = 600.0, count: int = 3) -> ClipSelectionRequest:
    return ClipSelectionRequest(
        transcript="привет мир это тест",
        segments=(
            TranscriptSegment(start=0.0, end=10.0, text="привет мир"),
            TranscriptSegment(start=10.0, end=30.0, text="это тест"),
        ),
        total_duration_seconds=total,
        target_count=count,
        min_seconds=20.0,
        max_seconds=60.0,
    )


# ---------------------------------------------------------------------------
# Validation logic tests (no network)
# ---------------------------------------------------------------------------

def test_validator_accepts_clean_payload():
    sel = OpenRouterClipSelector(api_key="k")
    req = _make_request()
    raw = json.dumps({
        "clips": [
            {"start": 30.0, "end": 80.0, "title": "А", "hook": "х", "reason": "r"},
            {"start": 100.0, "end": 150.0, "title": "Б", "hook": "х", "reason": "r"},
            {"start": 200.0, "end": 240.0, "title": "В", "hook": "х", "reason": "r"},
        ]
    })
    out = sel._parse_and_validate(raw, req)
    assert len(out) == 3
    assert out[0].start == 30.0


def test_validator_rejects_non_object():
    sel = OpenRouterClipSelector(api_key="k")
    with pytest.raises(ClipSelectorError, match="Expected object"):
        sel._parse_and_validate("[1,2,3]", _make_request())


def test_validator_rejects_missing_clips_key():
    sel = OpenRouterClipSelector(api_key="k")
    with pytest.raises(ClipSelectorError, match="'clips' array"):
        sel._parse_and_validate('{"foo": 1}', _make_request())


def test_validator_rejects_negative_start():
    sel = OpenRouterClipSelector(api_key="k")
    raw = json.dumps({"clips": [{"start": -1.0, "end": 10.0}]})
    with pytest.raises(ClipSelectorError, match="start < 0"):
        sel._parse_and_validate(raw, _make_request())


def test_validator_rejects_end_beyond_duration():
    sel = OpenRouterClipSelector(api_key="k")
    raw = json.dumps({"clips": [{"start": 100.0, "end": 9999.0}]})
    with pytest.raises(ClipSelectorError, match="end .* > video duration"):
        sel._parse_and_validate(raw, _make_request(total=600.0))


def test_validator_rejects_end_le_start():
    sel = OpenRouterClipSelector(api_key="k")
    raw = json.dumps({"clips": [{"start": 50.0, "end": 50.0}]})
    with pytest.raises(ClipSelectorError, match="end <= start"):
        sel._parse_and_validate(raw, _make_request())


def test_validator_rejects_too_long_clip():
    sel = OpenRouterClipSelector(api_key="k")
    raw = json.dumps({"clips": [{"start": 0.0, "end": 500.0}]})  # 500s
    with pytest.raises(ClipSelectorError, match="duration"):
        sel._parse_and_validate(raw, _make_request())


def test_validator_rejects_overlap():
    sel = OpenRouterClipSelector(api_key="k")
    raw = json.dumps({"clips": [
        {"start": 0.0, "end": 40.0},
        {"start": 25.0, "end": 55.0},  # overlaps heavily (20s overlap vs 20s min)
    ]})
    with pytest.raises(ClipSelectorError, match="overlap"):
        sel._parse_and_validate(raw, _make_request())


def test_validator_rejects_zero_clips():
    sel = OpenRouterClipSelector(api_key="k")
    with pytest.raises(ClipSelectorError, match="zero clips"):
        sel._parse_and_validate('{"clips": []}', _make_request())


def test_validator_rejects_invalid_json():
    sel = OpenRouterClipSelector(api_key="k")
    with pytest.raises(ClipSelectorError, match="Invalid JSON"):
        sel._parse_and_validate("not-json", _make_request())


# ---------------------------------------------------------------------------
# select_clips: HTTP integration (mocked)
# ---------------------------------------------------------------------------

def _mock_openrouter_response(payload: dict[str, Any]) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "choices": [{"message": {"content": json.dumps(payload)}}]
    }
    return resp


def test_select_clips_happy_path(monkeypatch):
    monkeypatch.setattr(
        "app.services.analysis.openrouter.get_settings",
        lambda: _settings(),
    )
    sel = OpenRouterClipSelector(api_key="k", model="test-model")
    client = MagicMock()
    client.is_closed = False
    client.post = AsyncMock(return_value=_mock_openrouter_response({
        "clips": [
            {"start": 30.0, "end": 80.0, "title": "А", "hook": "х", "reason": "r"},
            {"start": 100.0, "end": 150.0, "title": "Б", "hook": "х", "reason": "r"},
            {"start": 200.0, "end": 240.0, "title": "В", "hook": "х", "reason": "r"},
        ]
    }))

    async def _run():
        sel._client = client
        return await sel.select_clips(_make_request())

    result = asyncio.run(_run())
    assert isinstance(result, ClipSelection)
    assert len(result.clips) == 3
    assert result.model == "test-model"
    assert client.post.call_count == 1


def test_select_clips_retries_on_parse_error(monkeypatch):
    """Bad JSON on first call → second attempt succeeds."""
    monkeypatch.setattr(
        "app.services.analysis.openrouter.get_settings",
        lambda: _settings(),
    )
    sel = OpenRouterClipSelector(api_key="k", model="test-model", max_retries=1)

    bad = MagicMock()
    bad.status_code = 200
    bad.json.return_value = {"choices": [{"message": {"content": "garbage"}}]}

    good = _mock_openrouter_response({
        "clips": [{"start": 0.0, "end": 30.0, "title": "X", "hook": "h", "reason": "r"}]
    })

    client = MagicMock()
    client.is_closed = False
    client.post = AsyncMock(side_effect=[bad, good])

    async def _run():
        sel._client = client
        return await sel.select_clips(_make_request(count=1))

    result = asyncio.run(_run())
    assert len(result.clips) == 1
    assert client.post.call_count == 2  # first failed, second succeeded


def test_select_clips_raises_after_max_retries(monkeypatch):
    monkeypatch.setattr(
        "app.services.analysis.openrouter.get_settings",
        lambda: _settings(),
    )
    sel = OpenRouterClipSelector(api_key="k", model="test-model", max_retries=0)

    bad = MagicMock()
    bad.status_code = 500
    bad.text = "boom"

    client = MagicMock()
    client.is_closed = False
    client.post = AsyncMock(return_value=bad)

    async def _run():
        sel._client = client
        return await sel.select_clips(_make_request())

    with pytest.raises(ClipSelectorError, match="OpenRouter clip selection failed"):
        asyncio.run(_run())


# ---------------------------------------------------------------------------
# Analyser orchestration
# ---------------------------------------------------------------------------

class FakeSelector(ClipSelector):
    def __init__(self, clips: list[ClipCandidate]) -> None:
        self._clips = clips

    @property
    def name(self) -> str: return "fake"

    @property
    def model_id(self) -> str: return "fake-model"

    async def select_clips(self, request: ClipSelectionRequest) -> ClipSelection:
        return ClipSelection(clips=tuple(self._clips), model="fake-model")


def test_analyser_returns_clips():
    transcribed = TranscribedJob(
        language="ru",
        duration_seconds=600.0,
        segments=(Segment(start=0, end=10, text="x"),),
        raw_text="x",
        model="w",
    )
    fake = FakeSelector([
        ClipCandidate(start=10.0, end=40.0, title="A", hook="h", reason="r"),
        ClipCandidate(start=100.0, end=150.0, title="B", hook="h", reason="r"),
        ClipCandidate(start=200.0, end=250.0, title="C", hook="h", reason="r"),
    ])
    out = asyncio.run(Analyser(selector=fake).analyse(transcribed))
    assert len(out.clips) == 3


def test_analyser_truncates_extra_clips():
    """If the LLM returns more than target_count, we slice down."""
    transcribed = TranscribedJob(
        language="ru",
        duration_seconds=600.0,
        segments=(Segment(start=0, end=10, text="x"),),
        raw_text="x",
        model="w",
    )
    fake = FakeSelector([
        ClipCandidate(start=10.0, end=40.0, title=f"T{i}", hook="h", reason="r")
        for i in range(7)
    ])
    out = asyncio.run(
        Analyser(selector=fake, target_count=3).analyse(transcribed)
    )
    assert len(out.clips) == 3


def test_analyser_raises_on_empty():
    transcribed = TranscribedJob(
        language="ru",
        duration_seconds=600.0,
        segments=(Segment(start=0, end=10, text="x"),),
        raw_text="x",
        model="w",
    )
    fake = FakeSelector([])
    with pytest.raises(AnalyserError):
        asyncio.run(Analyser(selector=fake).analyse(transcribed))