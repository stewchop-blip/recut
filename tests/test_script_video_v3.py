"""Focused tests for Script Video Quality V3 hybrid media."""
from __future__ import annotations

import shutil
import subprocess

import pytest

from app.services.script_video.assembly import ScriptVideoAssembler
from app.services.script_video.hybrid_media import HybridMediaResolver, HybridMediaResult
from app.services.script_video.models import SceneSchema
from app.services.script_video.stock_video import (
    CoverrStockVideoProvider,
    StockVideoAsset,
    WikimediaCommonsStockVideoProvider,
)


def test_scene_schema_accepts_hybrid_media_hints():
    scene = SceneSchema(
        id="s1",
        narration="Он нашёл старую камеру.",
        visual_prompt="Vertical close-up of an old film camera on a flea market table",
        stock_query="old film camera",
        prefer_stock=True,
        duration_hint=3.5,
        importance=4,
        motion_type="zoom_in",
        transition="fade",
    )
    assert scene.stock_query == "old film camera"
    assert scene.prefer_stock is True


def test_coverr_rank_prefers_vertical_clip_with_similar_duration():
    vertical = {
        "duration": 4.2,
        "is_vertical": True,
        "max_width": 1080,
        "max_height": 1920,
    }
    landscape = {
        "duration": 4.0,
        "is_vertical": False,
        "max_width": 1920,
        "max_height": 1080,
    }
    assert (
        CoverrStockVideoProvider._rank_video(vertical, 4.0)
        > CoverrStockVideoProvider._rank_video(landscape, 4.0)
    )


def test_wikimedia_only_accepts_cc0_or_public_domain():
    safe = WikimediaCommonsStockVideoProvider._license_is_safe
    assert safe("CC0") is True
    assert safe("CC0 1.0 Universal") is True
    assert safe("Public domain") is True
    assert safe("Public Domain Mark") is True
    assert safe("CC BY 4.0") is False
    assert safe("CC BY-SA 4.0") is False
    assert safe("") is False


def test_hybrid_ai_budget_allows_at_most_one_reused_scene():
    resolver = object.__new__(HybridMediaResolver)
    resolver.stock_enabled = True
    resolver.max_ai_images = 4
    resolver.max_reused_scenes = 1
    assert resolver._ai_budget(2) == 2
    assert resolver._ai_budget(4) == 4
    assert resolver._ai_budget(7) == 6
    assert resolver._ai_budget(10) == 9


def test_hybrid_ai_budget_keeps_v2_quality_when_stock_disabled():
    resolver = object.__new__(HybridMediaResolver)
    resolver.stock_enabled = False
    resolver.max_ai_images = 4
    resolver.max_reused_scenes = 1
    assert resolver._ai_budget(8) == 8


def test_hybrid_media_attribution_is_deduplicated(tmp_path):
    assets = (
        StockVideoAsset(
            path=tmp_path / "one.mp4",
            provider="coverr",
            source_url="https://coverr.co",
            creator_name="Coverr",
            creator_url="https://coverr.co",
            asset_id="1",
        ),
        StockVideoAsset(
            path=tmp_path / "two.mp4",
            provider="coverr",
            source_url="https://coverr.co",
            creator_name="Coverr",
            creator_url="https://coverr.co",
            asset_id="2",
        ),
        StockVideoAsset(
            path=tmp_path / "three.webm",
            provider="wikimedia",
            source_url="https://commons.wikimedia.org/wiki/File:Example.webm",
            creator_name="Example",
            creator_url="https://commons.wikimedia.org",
            asset_id="3",
        ),
    )
    result = HybridMediaResult(
        paths=tuple(asset.path for asset in assets),
        stock_assets=assets,
        ai_images=0,
        stock_videos=3,
        reused_scenes=0,
    )
    assert result.attribution_text.count("Coverr") == 1
    assert result.attribution_text.count("Wikimedia Commons") == 1


@pytest.mark.asyncio
async def test_assembler_renders_stock_video_beat_to_vertical_mp4(tmp_path):
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("FFmpeg and ffprobe are required")

    stock = tmp_path / "stock.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=640x360:rate=30:duration=2",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(stock),
        ],
        check=True,
        timeout=30,
    )

    output = tmp_path / "beat.mp4"
    await ScriptVideoAssembler()._render_video_beat(
        stock,
        duration=1.5,
        output_path=output,
        beat_index=0,
    )
    probe = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height,codec_name,pix_fmt",
            "-of",
            "default=noprint_wrappers=1",
            str(output),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout
    assert "width=1080" in probe
    assert "height=1920" in probe
    assert "codec_name=h264" in probe
    assert "pix_fmt=yuv420p" in probe
