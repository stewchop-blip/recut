"""Quick tests for business logic — no real APIs."""

import pytest
from app.core.limits import LimitsManager
from app.core.config import get_settings


def test_text_length_check():
    lm = LimitsManager()
    settings = get_settings()
    ok = lm.check_text_length("hello")
    assert ok.allowed
    too_long = lm.check_text_length("x" * (settings.max_text_length + 1))
    assert not too_long.allowed


def test_voice_mapping():
    from app.services.tts.openrouter import OpenRouterTTSProvider
    p = OpenRouterTTSProvider()
    assert "male" in p.supported_voices
    assert p.get_provider_voice("female") == "nova"


def test_temp_cleanup():
    from app.utils.temp import TempFileManager
    tm = TempFileManager()
    # Just verify initialization
    assert tm.BASE_DIR.exists()


def test_rewriter_system_prompt():
    from app.services.text.rewriter import REWRITE_SYSTEM_PROMPT
    assert "فظ" in REWRITE_SYSTEM_PROMPT or "естественно" in REWRITE_SYSTEM_PROMPT