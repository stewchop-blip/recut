"""Story clock, safe reuse and no extra paid requests."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.script_video.assembly import ScriptVideoAssembler
from app.services.script_video.image_generator import ImageGenerator
from app.services.script_video.models import Scene, VideoPlan
from app.services.script_video.timeline import TimelineCompiler, VisualBeat


def plan():
    scenes = tuple(Scene(str(i), text, "Same person, blue mug", 5, importance,
                         motion, "fade", prefer_stock=False)
                   for i, (text, importance, motion) in enumerate([
                       ("Он открыл коробку.", 3, "pan_left"),
                       ("Внутри была кружка.", 5, "zoom_in"),
                       ("Не оборачивайся.", 5, "static"),
                   ]))
    return VideoPlan("История", "ru", 15, " ".join(s.narration for s in scenes),
                     "One short film", scenes)


def test_existing_subtitle_clock_controls_reveal_and_keeps_audio_tail():
    phrases = [SimpleNamespace(start=0.1, end=2, text="Он открыл коробку."),
               SimpleNamespace(start=2.4, end=8, text="Внутри была кружка."),
               SimpleNamespace(start=8.2, end=10, text="Не оборачивайся.")]
    beats = TimelineCompiler().compile(plan(), 3, 11, phrases=phrases)
    assert [b.duration for b in beats] == pytest.approx([2.2, 5.9, 2.9])
    assert all(b.transition == "cut" for b in beats)
    assert sum(b.duration for b in beats) == pytest.approx(11)


@pytest.mark.parametrize("phrases", [
    [SimpleNamespace(start=0, end=float("nan"), text="wrong")],
    [SimpleNamespace(start=0, end=40, text="wrong")],
    [SimpleNamespace(start=0, end=4, text="Чужой текст.")],
    [SimpleNamespace(start=-1, end=4, text="Он открыл коробку.")],
])
def test_invalid_or_unrelated_timing_uses_plan_without_new_alignment(phrases):
    beats = TimelineCompiler().compile(plan(), 3, 15, phrases=phrases)
    assert [b.duration for b in beats] == pytest.approx([5, 5, 5])


def test_reused_source_has_no_phantom_cuts_crops_or_reversed_camera():
    source = Path("/tmp/still.png")
    assets = [source, source, Path("/tmp/last.png")]
    beats = TimelineCompiler().compile(plan(), 3, 15, assets=assets)
    merged = ScriptVideoAssembler._merge_contiguous_beats(beats, assets)
    assert len(merged) == 2
    assert merged[0].duration == 10
    assert merged[0].motion == "zoom_in"  # The important object drives this one path.
    assert merged[1].motion == "static"
    assert merged[0].importance == 5
    assert all(b.transition == "cut" for b in merged)
    graph = ScriptVideoAssembler._motion_filter(merged[0], 0, 10)
    assert "crop=1080:1920" in graph  # No blind detail crop of an unlocated object.


def test_only_real_source_changes_receive_transition_overlap():
    p = plan()
    p = replace(p, scenes=tuple(replace(s, importance=3) for s in p.scenes))
    assets = [Path("a.png"), Path("a.png"), Path("b.png")]
    beats = TimelineCompiler().compile(p, 3, 15, assets=assets)
    assert [b.transition for b in beats] == ["cut", "cut", "fade"]
    assert sum(b.duration for b in beats) - .22 == pytest.approx(15)
    merged = ScriptVideoAssembler._merge_contiguous_beats(beats, assets)
    assert sum(b.duration for b in merged) - .22 == pytest.approx(15)


@pytest.mark.parametrize("duration", [float("nan"), float("inf"), -1])
def test_nonfinite_audio_cannot_create_invalid_ffmpeg_timeline(duration):
    with pytest.raises(ValueError):
        TimelineCompiler().compile(plan(), 3, duration)


@pytest.mark.asyncio
async def test_failed_generation_preserves_scene_slots_without_showing_future_reveal():
    class Client:
        calls = 0
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return None
        async def generate_image(self, **kwargs):
            self.calls += 1
            if kwargs["prompt"] == "middle":
                raise RuntimeError("provider failure")
            return [kwargs["prompt"]]
    client = Client()
    images = await ImageGenerator(client=client, fallback_model="", max_retries=1).generate(
        ["opening", "middle", "reveal"], preserve_slots=True,
    )
    assert images == ["opening", "", "reveal"]
    assert client.calls == 3  # Same capped requests, no repair generation.


@pytest.mark.asyncio
async def test_only_verified_empty_padding_allows_an_important_insert(tmp_path):
    from PIL import Image, ImageDraw
    padded = tmp_path / "padded.png"
    image = Image.new("RGB", (720, 1280), "black")
    ImageDraw.Draw(image).rectangle((80, 150, 640, 1130), fill="white")
    image.save(padded)
    beats = [VisualBeat("a", 0, 3, "zoom_in", "cut"),
             VisualBeat("b", 0, 3, "zoom_in", "cut", importance=5)]
    safe = ScriptVideoAssembler._safe_framing(beats, [padded])
    assert safe[0].framing_scale == 1
    assert safe[1].framing_scale == pytest.approx(1.08)
    assert len(ScriptVideoAssembler._merge_contiguous_beats(safe, [padded])) == 2
    import shutil
    if shutil.which("ffmpeg"):
        output = tmp_path / "safe-insert.mp4"
        await ScriptVideoAssembler()._render_image_beat(safe[1], padded, .2, output)
        assert output.stat().st_size > 0
    # A single foreground object at the edge must veto the crop, even with
    # the same scene importance and ample blank space everywhere else.
    ImageDraw.Draw(image).rectangle((0, 400, 10, 430), fill="red")
    image.save(padded)
    assert ScriptVideoAssembler._safe_framing(beats, [padded])[1].framing_scale == 1
    image.resize((512, 910)).save(padded)
    assert ScriptVideoAssembler._safe_framing(beats, [padded])[1].framing_scale == 1


def test_paid_keyframes_capture_nearby_reveal_without_losing_opening_or_payoff():
    from app.services.script_video.hybrid_media import HybridMediaResolver
    p = plan()
    scenes = tuple(replace(p.scenes[0], id=str(i), importance=5 if i == 4 else 1)
                   for i in range(10))
    p = replace(p, scenes=scenes)
    selected = HybridMediaResolver._select_ai_indices(p, list(range(10)), 4)
    assert selected == [0, 4, 6, 9]
    assert len(selected) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("always_bad", [False, True])
async def test_successful_encoder_with_broken_mp4_retries_once(tmp_path, monkeypatch, always_bad):
    from unittest.mock import AsyncMock
    import app.services.script_video.assembly as module
    from app.services.media.probe import ProbeError
    output = tmp_path / "beat.mp4"
    calls = 0
    async def spawn(*args, **kwargs):
        nonlocal calls
        calls += 1
        output.write_bytes(b"unfinished mp4")
        return SimpleNamespace(returncode=0)
    probe = AsyncMock(side_effect=[ProbeError("missing moov"),
                                 ProbeError("missing moov") if always_bad else object()])
    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(module, "get_probe_service", lambda: SimpleNamespace(probe=probe))
    monkeypatch.setattr(module.ScriptVideoAssembler, "_communicate", AsyncMock(return_value=(b"", b"")))
    if always_bad:
        with pytest.raises(module.ScriptVideoAssemblyError, match="failed validation"):
            await ScriptVideoAssembler()._run_beat_command(["ffmpeg"], output)
        assert not output.exists()
    else:
        await ScriptVideoAssembler()._run_beat_command(["ffmpeg"], output)
    assert calls == 2
