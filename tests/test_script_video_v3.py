"""Focused tests for Script Video Quality V3 hybrid media."""
from __future__ import annotations

import shutil
import subprocess
from types import SimpleNamespace

import pytest

from app.services.script_video.assembly import ScriptVideoAssembler
from app.services.script_video.hybrid_media import HybridMediaResolver, HybridMediaResult
from app.services.script_video.models import SceneSchema
from app.services.script_video.stock_video import (
    CoverrStockVideoProvider,
    StockVideoAsset,
    WikimediaCommonsStockVideoProvider,
)
from app.services.subtitles.scene_adapter import SceneSubtitleBuilder


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


def test_hybrid_ai_budget_is_a_hard_cap():
    resolver = object.__new__(HybridMediaResolver)
    resolver.max_ai_images = 4
    assert resolver._ai_budget(0) == 0
    assert resolver._ai_budget(2) == 2
    assert resolver._ai_budget(4) == 4
    assert resolver._ai_budget(7) == 4
    assert resolver._ai_budget(10) == 4


def test_hybrid_ai_budget_does_not_expand_when_stock_is_disabled():
    resolver = object.__new__(HybridMediaResolver)
    resolver.stock_enabled = False
    resolver.max_ai_images = 4
    assert resolver._ai_budget(8) == 4


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
async def test_stock_probe_rejects_non_video(tmp_path):
    broken = tmp_path / "broken.mp4"
    broken.write_text("not a video", encoding="utf-8")
    assert await HybridMediaResolver._validate_stock_video(broken) is False


@pytest.mark.asyncio
async def test_stock_probe_accepts_real_video(tmp_path):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("FFmpeg is required")
    stock = tmp_path / "stock.mp4"
    subprocess.run(
        [
            ffmpeg, "-y", "-v", "error", "-f", "lavfi", "-i",
            "testsrc2=size=360x640:rate=30:duration=1", "-c:v", "libx264",
            "-pix_fmt", "yuv420p", str(stock),
        ],
        check=True,
        timeout=30,
    )
    assert await HybridMediaResolver._validate_stock_video(stock) is True


def test_word_aligned_subtitles_use_real_timestamps_and_safe_margin(tmp_path, monkeypatch):
    import app.services.subtitles.scene_adapter as module

    settings = SimpleNamespace(
        subtitle_max_words_per_line=4,
        output_width=1080,
        output_height=1920,
        subtitle_safe_bottom_px=520,
        subtitle_safe_left_px=80,
        subtitle_safe_right_px=80,
    )
    monkeypatch.setattr(module, "get_settings", lambda: settings)
    words = [
        SimpleNamespace(start=0.25, end=0.55, text="Это"),
        SimpleNamespace(start=0.60, end=0.95, text="точный"),
        SimpleNamespace(start=1.02, end=1.40, text="тайминг."),
    ]
    result = SceneSubtitleBuilder().build_from_words(words)
    assert result.line_count == 1
    assert abs(result.phrases[0].start - 0.25) < 0.01
    assert abs(result.phrases[0].end - 1.40) < 0.01
    text = open(result.output_path, encoding="utf-8").read()
    assert ",80,80,520,1" in text
    assert "{\\k" in text


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


def test_pyav_version_is_compatible_with_released_faster_whisper():
    """Prevent Railway builds from silently upgrading to unsupported PyAV 18+."""
    import tomllib
    from pathlib import Path

    project = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    )
    requirements = project["project"]["dependencies"]
    assert any(req.replace(" ", "") == "av>=11,<18" for req in requirements)


@pytest.mark.asyncio
async def test_wikimedia_download_uses_identifiable_user_agent(tmp_path, monkeypatch):
    """The file download must not use default python-httpx identification."""
    import app.services.script_video.stock_video as stock_module

    captured = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    provider = WikimediaCommonsStockVideoProvider()
    async def no_matches(client, query):
        return []
    monkeypatch.setattr(provider, "_search", no_matches)
    monkeypatch.setattr(stock_module.httpx, "AsyncClient", FakeClient)
    assert await provider.fetch(
        query="old camera", workdir=tmp_path, slot=0, target_seconds=3.0,
    ) is None
    assert "ReCutBot" in captured["headers"]["User-Agent"]
    assert "github.com/stewchop-blip/recut" in captured["headers"]["User-Agent"]


def test_ffmpeg_xfade_chain_accepts_normalized_framerate(tmp_path):
    """Regression for FFmpeg 7.x: settb=AVTB alone loses constant frame rate."""
    from app.services.script_video.timeline import VisualBeat

    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("FFmpeg and ffprobe are required")
    videos = []
    for i, color in enumerate(("red", "blue", "green")):
        path = tmp_path / f"clip-{i}.mp4"
        subprocess.run(
            [ffmpeg, "-y", "-v", "error", "-f", "lavfi", "-i",
             f"color=c={color}:s=96x160:r=30:d=2", "-c:v", "libx264",
             "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(path)],
            check=True, timeout=20,
        )
        videos.append(path)

    beats = [
        VisualBeat("s1", 0, 2.0, "zoom_in", "cut"),
        VisualBeat("s2", 1, 2.0, "zoom_in", "fade"),
        VisualBeat("s3", 2, 2.0, "zoom_in", "fade"),
    ]
    graph = ScriptVideoAssembler()._filter_graph(beats, [2.0, 2.0, 2.0])
    output = tmp_path / "xfade.mp4"
    command = [ffmpeg, "-y", "-v", "error"]
    for path in videos:
        command += ["-i", str(path)]
    command += [
        "-filter_complex", graph, "-map", "[vout]", "-an",
        "-c:v", "libx264", "-preset", "ultrafast", "-t", "5.56", str(output),
    ]
    subprocess.run(command, check=True, timeout=30, capture_output=True)
    assert output.is_file() and output.stat().st_size > 0
    duration = float(subprocess.check_output(
        [ffprobe, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(output)],
        text=True,
    ).strip())
    assert 5.2 < duration <= 5.8


def test_ffmpeg_three_consecutive_xfades(tmp_path):
    """FFmpeg 7+ rejects xfade when input links lose their constant frame rate."""
    from app.services.script_video.timeline import VisualBeat

    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("FFmpeg/ffprobe not available")

    paths = []
    for index, color in enumerate(("red", "green", "blue")):
        path = tmp_path / f"xfade-{index}.mp4"
        subprocess.run(
            [
                ffmpeg, "-v", "error", "-f", "lavfi", "-i",
                f"color=c={color}:s=96x160:r=30:d=1",
                "-t", "1", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-y", str(path),
            ],
            capture_output=True, check=True, timeout=20,
        )
        paths.append(path)

    beats = [
        VisualBeat(scene_id=f"s{i}", image_index=i, duration=1.0,
                   motion="static", transition="cut" if i == 0 else "fade")
        for i in range(3)
    ]
    graph = ScriptVideoAssembler()._filter_graph(beats, [1.0] * 3)
    output = tmp_path / "three-xfades.mp4"
    command = [ffmpeg, "-v", "error"]
    for path in paths:
        command += ["-i", str(path)]
    command += [
        "-filter_complex", graph, "-map", "[vout]", "-t", "2.56",
        "-c:v", "libx264", "-y", str(output),
    ]
    subprocess.run(command, capture_output=True, check=True, timeout=20)
    meta = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(output)],
        capture_output=True, check=True, text=True, timeout=20,
    )
    import json
    duration = float(json.loads(meta.stdout)["format"]["duration"])
    assert 2.3 < duration < 2.9


def test_whisper_pyav_supports_audio_metadata_flag(tmp_path):
    """Catch incompatible newer PyAV before the deployment silently loses subtitles."""
    av = pytest.importorskip("av")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("FFmpeg not available")
    wav = tmp_path / "narration.wav"
    subprocess.run(
        [ffmpeg, "-v", "error", "-f", "lavfi", "-i",
         "sine=frequency=440:duration=0.5", "-y", str(wav)],
        capture_output=True, check=True, timeout=20,
    )
    with av.open(str(wav), mode="r", metadata_errors="ignore") as container:
        assert any(stream.type == "audio" for stream in container.streams)


def test_subtitles_keep_narration_text_not_whisper_guesses(tmp_path, monkeypatch):
    """A recognition error must not change words already spoken by the TTS."""
    import app.services.subtitles.scene_adapter as module

    settings = SimpleNamespace(
        subtitle_max_words_per_line=4,
        output_width=1080,
        output_height=1920,
        subtitle_safe_bottom_px=520,
        subtitle_safe_left_px=80,
        subtitle_safe_right_px=80,
    )
    monkeypatch.setattr(module, "get_settings", lambda: settings)
    original = "Маленькая девочка нашла старую плёнку у двери."
    recognized = [
        SimpleNamespace(start=i * 0.42, end=i * 0.42 + 0.30, text=word)
        for i, word in enumerate(
            ("Маленькая", "девочка", "нашла", "старую", "пленку", "у", "дверей.")
        )
    ]
    result = SceneSubtitleBuilder().build_from_script_and_words(original, recognized)
    assert result.output_path
    assert " ".join(phrase.text for phrase in result.phrases) == original
    assert all("дверей" not in phrase.text for phrase in result.phrases)
    assert abs(result.phrases[0].start - 0.0) < 0.02
    assert abs(result.phrases[-1].end - 2.82) < 0.08
    text = open(result.output_path, encoding="utf-8").read()
    assert "плёнку" in text
    assert "дверей" not in text


def test_subtitles_discard_unrelated_whisper_hallucination(tmp_path, monkeypatch):
    import app.services.subtitles.scene_adapter as module

    monkeypatch.setattr(
        module, "get_settings", lambda: SimpleNamespace(
            subtitle_max_words_per_line=4,
            output_width=1080,
            output_height=1920,
            subtitle_safe_bottom_px=520,
            subtitle_safe_left_px=80,
            subtitle_safe_right_px=80,
        )
    )
    result = SceneSubtitleBuilder().build_from_script_and_words(
        "Эта таинственная история началась в старом городе.",
        [SimpleNamespace(start=0.1, end=0.4, text="Подписывайтесь")],
    )
    assert not result.output_path


def test_reused_still_shares_one_zoom_trajectory():
    """Two independently rendered cuts into the same file must not restart zoom."""
    from pathlib import Path
    from app.services.script_video.timeline import VisualBeat

    still = Path("/tmp/repeated-still.png")
    stock = Path("/tmp/stock.mp4")
    assets = [still, still, stock]
    beats = [
        VisualBeat(scene_id="s1", image_index=0, duration=2.0,
                   motion="zoom_in", transition="cut"),
        VisualBeat(scene_id="s2", image_index=1, duration=2.0,
                   motion="pan_right", transition="cut"),
        VisualBeat(scene_id="s3", image_index=2, duration=1.0,
                   motion="static", transition="cut"),
    ]
    windows = ScriptVideoAssembler._still_motion_windows(beats, assets, [2.0, 2.0, 1.0])
    assert windows[:2] == [(0, 120, "zoom_in"), (60, 120, "zoom_in")]
    first = ScriptVideoAssembler._motion_filter(
        beats[0], 0, 2.0,
        start_frame=windows[0][0], total_frames=windows[0][1],
        continuous_motion=windows[0][2],
    )
    second = ScriptVideoAssembler._motion_filter(
        beats[1], 0, 2.0,
        start_frame=windows[1][0], total_frames=windows[1][1],
        continuous_motion=windows[1][2],
    )
    assert "(on+0)/120" in first
    assert "(on+60)/120" in second
    assert "(on+0)/60" not in second
    assert "pan_right" not in second
    assert windows[2][0] == 0


def test_visual_relevance_rejects_unrelated_found_footage():
    from app.services.script_video.visual_policy import (
        stock_title_matches_query,
        is_narrative_request,
    )
    assert is_narrative_request("Придумай трогательную историю о старом фотоаппарате")
    assert is_narrative_request("A short mystery story about a lost film camera")
    assert not is_narrative_request("Почему люди откладывают важные дела")
    assert stock_title_matches_query(
        "old film camera", "Old film camera in vintage flea market"
    )
    assert stock_title_matches_query(
        "hands cooking pasta", "Close-up of hands cooking pasta"
    )
    assert not stock_title_matches_query(
        "old film camera", "ISS Mission Manager speaks about rocket launch"
    )
    assert not stock_title_matches_query(
        "family birthday party", "Cake TV advertising commercial with logo"
    )
    assert not stock_title_matches_query("family birthday party", "")
    assert not stock_title_matches_query("house", "Unrelated television documentary")


def test_stock_provider_descriptions_and_story_safety():
    from app.services.script_video.stock_video import PexelsStockVideoProvider
    from app.services.script_video.visual_policy import (
        narrative_plan_requires_visual_continuity,
    )
    assert PexelsStockVideoProvider._description({
        "url": "https://www.pexels.com/video/hands-developing-old-film-1234567/"
    }) == "hands developing old film"
    assert PexelsStockVideoProvider._description({
        "url": "https://www.pexels.com/video/1234567/"
    }) == ""
    plot = SimpleNamespace(scenes=[
        SimpleNamespace(reference_scene_id="s1"),
        SimpleNamespace(reference_scene_id="s1"),
    ])
    assert narrative_plan_requires_visual_continuity("Про камеру", plot)
    assert narrative_plan_requires_visual_continuity("Сочини мистическую историю", SimpleNamespace(scenes=[]))
    assert not narrative_plan_requires_visual_continuity(
        "Как научиться готовить", SimpleNamespace(scenes=[])
    )


@pytest.mark.asyncio
async def test_fictional_story_never_searches_stock_even_if_planner_prefers_it(
    tmp_path, monkeypatch
):
    """No unrelated news/cooking footage may enter a continuity-critical story."""
    import base64
    from app.services.script_video.image_generator import ImageGenerator
    from app.services.script_video.models import Scene, VideoPlan

    class ForbiddenStock:
        async def fetch(self, **kwargs):
            raise AssertionError("A fictional story must never search public stock")

    async def fake_images(self, prompts, **kwargs):
        return ["data:image/png;base64," + base64.b64encode(b"test-frame").decode()] * len(prompts)

    monkeypatch.setattr(ImageGenerator, "generate", fake_images)
    scenes = tuple(Scene(
        id=f"s{i}", narration=f"Сцена {i}.", visual_prompt="Cinematic film camera",
        stock_query="old film camera", prefer_stock=True, duration_hint=4,
        importance=3, motion_type="zoom_in", transition="cut"
    ) for i in range(8))
    plan = VideoPlan(
        title="Камера с секретом", language="ru", target_duration=40,
        narration=" ".join(s.narration for s in scenes), visual_style="Cinematic",
        scenes=scenes,
    )
    resolver = object.__new__(HybridMediaResolver)
    resolver.stock = ForbiddenStock()
    resolver.stock_enabled = True
    resolver.image_model = "recraft/recraft-v4.1-flash"
    resolver.fallback_image_model = ""
    resolver.max_ai_images = 4
    resolver.cost_tracker = None
    resolver.image_concurrency = 1
    result = await resolver.resolve(
        plan, tmp_path, user_idea="Сделай мистическую историю про старую камеру"
    )
    assert result.stock_videos == 0
    assert result.ai_images == 4
    assert len(result.paths) == 8
    assert result.reused_scenes == 4
