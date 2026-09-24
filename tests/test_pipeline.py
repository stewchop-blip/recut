"""Tests for the new pipeline pieces (no network, no subprocesses)."""
import os
import pytest

from app.core.config import Settings
from app.pipeline.validator import (
    SUPPORTED_MIME_TYPES,
    VideoMeta,
    VideoValidationError,
    VideoValidator,
)


def _settings(**overrides) -> Settings:
    base = dict(
        telegram_bot_token="t",
        openrouter_api_key="k",
        database_url="postgresql+asyncpg://x",
    )
    base.update(overrides)
    return Settings(**base)


def test_validator_check_size_ok():
    v = VideoValidator(max_size_bytes=10_000, max_duration_seconds=600, allowed_mime_types=None)
    v.check_size(9_999)


def test_validator_check_size_zero():
    v = VideoValidator(max_size_bytes=10_000, max_duration_seconds=600, allowed_mime_types=None)
    with pytest.raises(VideoValidationError) as ei:
        v.check_size(0)
    assert ei.value.code == "EMPTY_FILE"


def test_validator_check_size_too_big():
    v = VideoValidator(max_size_bytes=10_000, max_duration_seconds=600, allowed_mime_types=None)
    with pytest.raises(VideoValidationError) as ei:
        v.check_size(11_000)
    assert ei.value.code == "FILE_TOO_LARGE"


def test_validator_check_mime_allowed():
    v = VideoValidator(max_size_bytes=10_000, max_duration_seconds=600, allowed_mime_types=None)
    v.check_mime("video/mp4")
    v.check_mime(None)  # tolerated


def test_validator_check_mime_unsupported():
    v = VideoValidator(max_size_bytes=10_000, max_duration_seconds=600, allowed_mime_types=None)
    with pytest.raises(VideoValidationError) as ei:
        v.check_mime("video/x-fictional")
    assert ei.value.code == "UNSUPPORTED_FORMAT"


def test_validator_check_mime_octet_stream_tolerated():
    v = VideoValidator(max_size_bytes=10_000, max_duration_seconds=600, allowed_mime_types=None)
    v.check_mime("application/octet-stream")


def test_validator_check_duration_too_long():
    v = VideoValidator(max_size_bytes=10_000, max_duration_seconds=600, allowed_mime_types=None)
    with pytest.raises(VideoValidationError) as ei:
        v.check_duration(700)
    assert ei.value.code == "TOO_LONG"


def test_validator_check_duration_zero():
    v = VideoValidator(max_size_bytes=10_000, max_duration_seconds=600, allowed_mime_types=None)
    with pytest.raises(VideoValidationError) as ei:
        v.check_duration(0)
    assert ei.value.code == "ZERO_DURATION"


def test_validator_check_meta_ok():
    v = VideoValidator(max_size_bytes=10_000, max_duration_seconds=600, allowed_mime_types=None)
    meta = VideoMeta(
        duration_seconds=120.0,
        width=1920,
        height=1080,
        has_audio=True,
        mime_type="video/mp4",
    )
    v.check_meta(meta)


def test_settings_defaults_used_when_no_overrides(monkeypatch):
    """VideoValidator must respect Settings defaults when constructed without args."""
    s = _settings()
    v = VideoValidator()
    assert v._max_size == s.max_video_size_mb * 1024 * 1024
    assert v._max_duration == s.max_video_duration_minutes * 60


def test_supported_mime_types_includes_mp4():
    assert "video/mp4" in SUPPORTED_MIME_TYPES
    assert "video/quicktime" in SUPPORTED_MIME_TYPES


def test_ffprobe_rotation_parser_tolerates_missing_tag():
    from app.services.media.probe import _parse_rotation
    assert _parse_rotation({}) == 0
    assert _parse_rotation({"tags": {"rotate": "90"}}) == 90
    assert _parse_rotation({"tags": {"rotate": "180"}}) == 180
    assert _parse_rotation({"tags": {"rotate": "270"}}) == 270
    assert _parse_rotation({"tags": {"rotate": "999"}}) == 0  # invalid → 0
    assert _parse_rotation({"tags": {"rotate": "abc"}}) == 0
    # displaymatrix (side_data_list) — PART 5
    assert _parse_rotation({"side_data_list": [{"rotation": -90}]}) == 90
    assert _parse_rotation({"side_data_list": [{"rotation": 90}]}) == 270
    # rotate tag wins over displaymatrix
    assert _parse_rotation({
        "tags": {"rotate": "90"},
        "side_data_list": [{"rotation": 0}],
    }) == 90


def test_ffprobe_fps_parser():
    from app.services.media.probe import _parse_fps
    assert _parse_fps("30/1") == pytest.approx(30.0)
    assert _parse_fps("60000/1001") == pytest.approx(59.94, abs=1e-2)
    assert _parse_fps(None) == 0.0
    assert _parse_fps("bogus") == 0.0