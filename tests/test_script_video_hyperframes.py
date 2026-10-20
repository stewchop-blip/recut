"""Isolated HyperFrames pilot: no paid APIs and no Telegram side effects."""
from __future__ import annotations

import base64
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.script_video.assembly import ScriptVideoAssembler
from app.services.script_video.timeline import VisualBeat
from app.services.script_video.hyperframes_graphics import (
    HyperframesGraphicRenderer,
    MAX_SCENES,
)
from app.services.script_video.hybrid_media import HybridMediaResolver
from app.services.script_video.models import Scene, VideoPlan


def make_plan(*, story: bool = False) -> VideoPlan:
    scenes = tuple(
        Scene(
            id=f"s{i}",
            narration=f"Это полезный короткий факт номер {i} для зрителя.",
            visual_prompt=f"Graphic explaining scene {i}",
            stock_query="time management work",
            prefer_stock=not story,
            duration_hint=4.0,
            importance=3,
            motion_type="zoom_in",
            transition="cut",
        )
        for i in range(8)
    )
    return VideoPlan(
        title="Внимание и время", language="ru", target_duration=32,
        narration=" ".join(s.narration for s in scenes),
        visual_style="Minimal", scenes=scenes,
    )


def test_hyperframes_default_is_off_even_if_binary_is_present(monkeypatch):
    monkeypatch.delenv("SCRIPT_VIDEO_HYPERFRAMES_ENABLED", raising=False)
    monkeypatch.setattr(
        "app.services.script_video.hyperframes_graphics.shutil.which",
        lambda command: "/usr/local/bin/hyperframes",
    )
    assert HyperframesGraphicRenderer().available is False
    assert HyperframesGraphicRenderer(enabled=True).available is True


def test_hyperframes_selector_only_picks_explainer_beats():
    assert HyperframesGraphicRenderer.selected_indices(make_plan().scenes) == (2, 5)
    assert len(HyperframesGraphicRenderer.selected_indices(make_plan().scenes)) <= MAX_SCENES
    assert HyperframesGraphicRenderer.selected_indices(make_plan(story=True).scenes) == ()


def test_hyperframes_text_is_html_escaped_and_no_external_script():
    rendered = HyperframesGraphicRenderer.composition_html(
        title='<img src=x onerror="alert(1)">',
        narration='Вещь <script>alert("x")</script> полезна и доступна.',
        duration=3.8,
    )
    assert '<script>alert' not in rendered
    assert "<img" not in rendered
    assert "&lt;img" in rendered
    assert "&lt;script&gt;" in rendered
    assert "data-composition-id=" in rendered
    assert "data-no-timeline" in rendered
    assert "data-duration=\"3.800\"" in rendered
    assert "https://" not in rendered


@pytest.mark.asyncio
async def test_disabled_renderer_does_not_start_process(tmp_path, monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("Disabled HyperFrames must never spawn a subprocess")
    monkeypatch.setattr(
        "app.services.script_video.hyperframes_graphics.asyncio.create_subprocess_exec",
        forbidden,
    )
    renderer = HyperframesGraphicRenderer(enabled=False)
    assert await renderer.render_scene(
        title="Тема", narration="Это действительно полезное объяснение",
        duration=3, workdir=tmp_path, index=0,
    ) is None


@pytest.mark.asyncio
async def test_render_failure_falls_back_to_existing_ai_and_stock(tmp_path, monkeypatch):
    from app.services.script_video.image_generator import ImageGenerator

    class BrokenGraphics:
        available = True
        selected_indices = staticmethod(HyperframesGraphicRenderer.selected_indices)
        async def render_scene(self, **kwargs):
            return None

    generated_prompts = []
    async def fake_generate(self, prompts, **kwargs):
        generated_prompts.extend(prompts)
        return [
            "data:image/png;base64," + base64.b64encode(b"image" + bytes([i])).decode()
            for i in range(len(prompts))
        ]

    monkeypatch.setattr(
        "app.services.script_video.hybrid_media.HyperframesGraphicRenderer",
        BrokenGraphics,
    )
    monkeypatch.setattr(ImageGenerator, "generate", fake_generate)

    resolver = object.__new__(HybridMediaResolver)
    resolver.stock_enabled = False
    resolver.image_model = "recraft/recraft-v4.1-flash"
    resolver.fallback_image_model = ""
    resolver.max_ai_images = 4
    resolver.cost_tracker = None
    resolver.image_concurrency = 1

    media = await resolver.resolve(
        make_plan(), tmp_path, user_idea="Почему мы откладываем дела",
        test_hyperframes=True,
    )
    assert media.graphics_videos == 0
    assert media.ai_images == 4
    assert len(media.paths) == 8
    assert all(p.suffix in {".png", ".jpg"} for p in media.paths)
    assert len(generated_prompts) == 4


@pytest.mark.asyncio
async def test_successful_graphics_replace_before_paid_ai_calls(tmp_path, monkeypatch):
    from app.services.script_video.image_generator import ImageGenerator

    class FakeGraphics:
        available = True
        selected_indices = staticmethod(HyperframesGraphicRenderer.selected_indices)
        async def render_scene(self, **kwargs):
            path = kwargs["workdir"] / f"hyperframes-{kwargs['index']:02d}.mp4"
            path.write_bytes(b"not-real-media")
            return path

    async def mock_probe(path):
        return path.suffix == ".mp4"

    generated_prompts = []
    async def fake_generate(self, prompts, **kwargs):
        generated_prompts.extend(prompts)
        return [
            "data:image/png;base64," + base64.b64encode(b"image" + bytes([i])).decode()
            for i in range(len(prompts))
        ]

    monkeypatch.setattr(
        "app.services.script_video.hybrid_media.HyperframesGraphicRenderer",
        FakeGraphics,
    )
    monkeypatch.setattr(HybridMediaResolver, "_validate_stock_video", staticmethod(mock_probe))
    monkeypatch.setattr(ImageGenerator, "generate", fake_generate)

    resolver = object.__new__(HybridMediaResolver)
    resolver.stock_enabled = False
    resolver.image_model = "recraft/recraft-v4.1-flash"
    resolver.fallback_image_model = ""
    resolver.max_ai_images = 4
    resolver.cost_tracker = None
    resolver.image_concurrency = 1
    media = await resolver.resolve(
        make_plan(), tmp_path, user_idea="Почему люди откладывают важные дела",
        test_hyperframes=True,
    )
    assert media.graphics_videos == 2
    assert media.ai_images <= 4
    assert media.stock_videos == 0
    assert len(media.paths) == 8
    assert media.paths[2].suffix == ".mp4"
    assert media.paths[5].suffix == ".mp4"
    assert len(generated_prompts) == media.ai_images


@pytest.mark.asyncio
async def test_public_explainer_never_spawns_graphics(tmp_path, monkeypatch):
    """An enabled global flag must not opt regular users into Chromium."""
    from app.services.script_video.image_generator import ImageGenerator

    class Forbidden:
        def __init__(self, *args, **kwargs):
            raise AssertionError("Public requests must not call HyperFrames")

    async def fake_generate(self, prompts, **kwargs):
        return [
            "data:image/png;base64," + base64.b64encode(b"AI").decode()
            for _ in prompts
        ]

    monkeypatch.setenv("SCRIPT_VIDEO_HYPERFRAMES_ENABLED", "true")
    monkeypatch.setattr(
        "app.services.script_video.hybrid_media.HyperframesGraphicRenderer",
        Forbidden,
    )
    monkeypatch.setattr(ImageGenerator, "generate", fake_generate)
    resolver = object.__new__(HybridMediaResolver)
    resolver.stock_enabled = False
    resolver.image_model = "recraft/recraft-v4.1-flash"
    resolver.fallback_image_model = ""
    resolver.max_ai_images = 4
    resolver.cost_tracker = None
    resolver.image_concurrency = 1
    media = await resolver.resolve(
        make_plan(), tmp_path, user_idea="Почему люди откладывают дела"
    )
    assert media.graphics_videos == 0
    assert all(p.suffix in {".png", ".jpg"} for p in media.paths)


@pytest.mark.asyncio
async def test_narratives_never_spawn_graphics(tmp_path, monkeypatch):
    from app.services.script_video.image_generator import ImageGenerator

    class Forbidden:
        def __init__(self, *args, **kwargs):
            raise AssertionError("Narrative script must not call HyperFrames")

    async def fake_generate(self, prompts, **kwargs):
        return [
            "data:image/png;base64," + base64.b64encode(b"AI").decode()
            for _ in prompts
        ]

    monkeypatch.setattr(
        "app.services.script_video.hybrid_media.HyperframesGraphicRenderer",
        Forbidden,
    )
    monkeypatch.setattr(ImageGenerator, "generate", fake_generate)
    resolver = object.__new__(HybridMediaResolver)
    resolver.stock_enabled = False
    resolver.image_model = "recraft/recraft-v4.1-flash"
    resolver.fallback_image_model = ""
    resolver.max_ai_images = 4
    resolver.cost_tracker = None
    resolver.image_concurrency = 1

    media = await resolver.resolve(
        make_plan(story=True), tmp_path,
        user_idea="Придумай мистическую историю про плёночную камеру",
        test_hyperframes=True,
    )
    assert media.graphics_videos == 0
    assert media.ai_images == 4
    assert len(media.paths) == 8


def test_graphic_headline_split_preserves_words_and_readable_phrases():
    narration = "Почему мы откладываем даже важные дела и как это изменить?"
    lead, tail = HyperframesGraphicRenderer._headline_parts(narration)
    assert lead == "Почему мы откладываем даже важные дела"
    assert tail == "и как это изменить?"
    assert lead + " " + tail == narration
    assert not lead.endswith(" и")
    assert HyperframesGraphicRenderer._headline_parts("Один два") == ("Один два", "")

def test_hyperframes_reuse_never_loops_into_missing_explainer_scene(tmp_path):
    """A completed graphic may appear once, not be replayed as a filler clip."""
    still = tmp_path / "scene-00-ai.png"
    graphic = tmp_path / "hyperframes-02.mp4"
    still.write_bytes(b"still")
    graphic.write_bytes(b"graphic")
    assets = [still, None, graphic, None, None]
    assert HybridMediaResolver._nearest_without_graphics(assets, 3) == still
    assert HybridMediaResolver._nearest_without_graphics(assets, 4) == still
    assert HybridMediaResolver._nearest_without_graphics(assets, 1) == still
    # Ordinary still image reuse is unchanged.
    assert HybridMediaResolver._nearest_path(assets, 3) == graphic


def test_hyperframes_editor_keeps_one_continuous_graphic_beat(tmp_path):
    graphic = tmp_path / "hyperframes-02.mp4"
    still = tmp_path / "scene-00-ai.png"
    stock = tmp_path / "stock.mp4"
    assets = [still, graphic, stock]
    beats = [
        VisualBeat("s0", 0, 1.0, "zoom_in", "cut"),
        VisualBeat("s1", 1, 1.9, "static", "fade"),
        VisualBeat("s1", 1, 1.9, "zoom_in", "cut"),
        VisualBeat("s2", 2, 2.0, "static", "cut"),
        VisualBeat("s2", 2, 2.0, "static", "cut"),
        VisualBeat("s3", 1, 1.2, "static", "cut"),
    ]
    merged = ScriptVideoAssembler._merge_hyperframes_beats(beats, assets)
    assert len(merged) == 5
    assert merged[1] == VisualBeat("s1", 1, 3.8, "static", "fade")
    assert merged[2:4] == beats[3:5]  # Stock-video handling unchanged.
    assert merged[4] == beats[5]  # A separate scene does not get merged.
    assert ScriptVideoAssembler._merge_hyperframes_beats(
        beats[:1], assets
    ) == beats[:1]
