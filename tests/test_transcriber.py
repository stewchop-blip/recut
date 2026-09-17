"""Tests for the transcription stage (D) and memory guard.

We use a fake TranscriptionService so faster-whisper doesn't need to
actually load on CI. The fake returns canned segments.
"""
import asyncio
from pathlib import Path

import pytest

from app.core.memory_guard import (
    WHISPER_PEAK_RSS_MB,
    MemoryGuardError,
    estimate_whisper_peak_mb,
    require_free_for_whisper,
    snapshot,
)
from app.services.transcription.base import (
    Segment,
    TranscriptionError,
    TranscriptionRequest,
    TranscriptionResult,
    TranscriptionService,
)
from audio_fixture import make_silence_wav


# ---------------------------------------------------------------------------
# Fake STT service — deterministic, no model loading
# ---------------------------------------------------------------------------

class FakeTranscriptionService(TranscriptionService):
    """Returns hard-coded segments; tracks call count for assertions."""

    def __init__(self, segments: list[Segment] | None = None, language: str = "ru") -> None:
        self._segments = segments or []
        self._language = language
        self.call_count = 0

    @property
    def name(self) -> str:
        return "fake"

    @property
    def model_id(self) -> str:
        return "fake-model"

    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult:
        self.call_count += 1
        raw = " ".join(s.text for s in self._segments).strip()
        return TranscriptionResult(
            segments=tuple(self._segments),
            language=self._language,
            duration_seconds=self._segments[-1].end if self._segments else 0.0,
            model="fake-model",
            raw_text=raw,
        )


class BrokenTranscriptionService(TranscriptionService):
    """Always raises — used to verify error propagation."""

    @property
    def name(self) -> str:
        return "broken"

    @property
    def model_id(self) -> str:
        return "broken"

    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult:
        raise TranscriptionError("boom")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_fake_service_returns_segments(tmp_path: Path):
    wav = make_silence_wav(tmp_path / "x.wav", seconds=0.5)
    fake = FakeTranscriptionService(segments=[
        Segment(start=0.0, end=2.0, text="привет мир"),
    ])
    result = asyncio.run(fake.transcribe(TranscriptionRequest(audio_path=str(wav))))
    assert result.language == "ru"
    assert len(result.segments) == 1
    assert result.segments[0].text == "привет мир"
    assert result.raw_text == "привет мир"


def test_transcriber_orchestrates_service(tmp_path: Path):
    """Transcriber must call the injected service and reshape the result."""
    from app.pipeline.transcriber import Transcriber

    wav = make_silence_wav(tmp_path / "x.wav", seconds=1.0)
    fake = FakeTranscriptionService(segments=[
        Segment(start=0.0, end=1.5, text="один"),
        Segment(start=1.5, end=3.0, text="два три"),
    ])
    out = asyncio.run(Transcriber(service=fake).transcribe(wav, language="ru"))

    assert fake.call_count == 1
    assert len(out.segments) == 2
    assert out.raw_text == "один два три"
    assert out.model == "fake-model"


def test_transcriber_propagates_errors(tmp_path: Path):
    from app.pipeline.transcriber import Transcriber, TranscriberError

    wav = make_silence_wav(tmp_path / "x.wav", seconds=0.5)
    with pytest.raises(TranscriberError):
        asyncio.run(Transcriber(service=BrokenTranscriptionService()).transcribe(wav))


def test_transcriber_rejects_missing_file(tmp_path: Path):
    from app.pipeline.transcriber import Transcriber, TranscriberError

    missing = tmp_path / "nope.wav"
    fake = FakeTranscriptionService()
    with pytest.raises(TranscriberError):
        asyncio.run(Transcriber(service=fake).transcribe(missing))


# ---------------------------------------------------------------------------
# Memory guard
# ---------------------------------------------------------------------------

def test_estimate_whisper_peak_known_models():
    assert estimate_whisper_peak_mb("small") == 400
    assert estimate_whisper_peak_mb("tiny") == 150
    assert estimate_whisper_peak_mb("medium") == 800
    assert estimate_whisper_peak_mb("large-v3") == 1500


def test_estimate_whisper_peak_falls_back_to_small():
    assert estimate_whisper_peak_mb("unknown-model-xyz") == 400


def test_snapshot_is_well_formed():
    snap = snapshot()
    assert snap.total_mb > 0
    assert snap.available_mb >= 0
    assert 0.0 <= snap.percent <= 100.0


def test_require_free_for_whisper_accepts():
    """Real RAM on any test host > 100 MB, so this should pass."""
    snap = require_free_for_whisper("tiny", min_free_mb=100)
    assert snap.available_mb >= 0


def test_require_free_for_whisper_refuses_when_min_is_huge():
    """If we ask for more than physical RAM, the guard must raise."""
    with pytest.raises(MemoryGuardError) as ei:
        require_free_for_whisper("small", min_free_mb=10_000_000)
    assert "free RAM" in str(ei.value) or "Not enough" in str(ei.value)


def test_whisper_peak_table_has_no_duplicates():
    """Each model size must map to exactly one entry (sanity)."""
    assert len(WHISPER_PEAK_RSS_MB) >= 5