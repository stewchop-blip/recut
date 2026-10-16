"""Focused tests for Script Video Quality V3 hybrid media."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from app.services.script_video.assembly import ScriptVideoAssembler
from app.services.script_video.hybrid_media import HybridMediaResult
from app.services.script_video.models import SceneSchema
from app.services.script_video.stock_video import (
    CoverrStockVideoProvider,
    StockVideoAsset,
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
    )
    result = HybridMediaResult(
        paths=tuple(asset.path for asset in assets),
        stock_assets=assets,
        ai_images=0,
        stock_videos=2,
        reused_scenes=0,
    )
    assert result.attribution_text.count("Coverr") == 1


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
