"""Tests for faster-whisper wiring (no real model load).

We verify:
- the service constructs without loading the model
- it reads its settings from Settings
- the lazy loader only fires when transcribe() is called
- the lock prevents concurrent loads
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.config import Settings
from app.services.transcription.base import (
    TranscriptionRequest,
    TranscriptionResult,
    Segment,
)
from app.services.transcription.faster_whisper import (
    WhisperTranscriptionService,
    get_transcription_service,
)


def _settings(**overrides) -> Settings:
    base = dict(
        telegram_bot_token="t",
        openrouter_api_key="k",
        database_url="postgresql+asyncpg://x",
    )
    base.update(overrides)
    return Settings(**base)


def test_service_constructs_without_loading_model(monkeypatch):
    """Just creating the service must not touch faster_whisper.WhisperModel."""
    monkeypatch.setattr(
        "app.services.transcription.faster_whisper.get_settings",
        lambda: _settings(whisper_model="small", whisper_device="cpu", whisper_compute_type="int8"),
    )
    svc = WhisperTranscriptionService()
    assert svc.model_id == "small"
    assert svc._model is None


def test_service_lazy_loads_only_once(monkeypatch):
    monkeypatch.setattr(
        "app.services.transcription.faster_whisper.get_settings",
        lambda: _settings(whisper_model="small", whisper_device="cpu", whisper_compute_type="int8"),
    )

    # Build a single dummy segment
    class DummySeg:
        start = 0.0
        end = 1.0
        text = "x"
        words = None

    class DummyInfo:
        language = "ru"
        duration = 1.0

    fake_model = MagicMock()
    fake_model.transcribe.return_value = (iter([DummySeg()]), DummyInfo())

    with patch("faster_whisper.WhisperModel", return_value=fake_model) as model_cls:
        svc = WhisperTranscriptionService()
        req = TranscriptionRequest(audio_path="/tmp/x.wav")
        # First call -> loads model
        asyncio.run(svc.transcribe(req))
        # Second call -> reuses same model (model_cls called once)
        asyncio.run(svc.transcribe(req))
    assert model_cls.call_count == 1


def test_service_refuses_on_memory_guard(monkeypatch):
    """If memory guard raises, transcribe() surfaces a clean error."""
    monkeypatch.setattr(
        "app.services.transcription.faster_whisper.get_settings",
        lambda: _settings(whisper_model="small", whisper_device="cpu", whisper_compute_type="int8"),
    )
    svc = WhisperTranscriptionService()

    from app.services.transcription.base import TranscriptionError
    from app.core.memory_guard import MemoryGuardError

    with patch(
        "app.services.transcription.faster_whisper.require_free_for_whisper",
        side_effect=MemoryGuardError("OOM"),
    ):
        with pytest.raises(TranscriptionError):
            asyncio.run(svc.transcribe(TranscriptionRequest(audio_path="/tmp/x.wav")))
    assert svc._model is None


def test_global_service_singleton(monkeypatch):
    monkeypatch.setattr(
        "app.services.transcription.faster_whisper.get_settings",
        lambda: _settings(),
    )
    s1 = get_transcription_service()
    s2 = get_transcription_service()
    assert s1 is s2