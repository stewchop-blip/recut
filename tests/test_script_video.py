"""Tests for script video generation pipeline."""
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.script_video import CostTracker, ScriptPlanner, VideoPlanSchema, from_schema
from app.services.script_video.assembly import ScriptVideoAssembler, ScriptVideoAssemblyError
from app.services.script_video.image_generator import ImageCreditsExhaustedError, ImageGenerator
from app.services.script_video.models import Scene, VideoPlan
from app.services.script_video.timeline import TimelineCompiler
from app.services.script_video.tts_adapter import ScriptTTSAdapter


class _PlannerClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def chat_completion(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


def _planner_response(content, finish_reason="stop"):
    return {"choices": [{"finish_reason": finish_reason, "message": {"content": content}}]}


def _valid_planner_json():
    scene_narrations = [
        "Сначала кажется, что проблема просто в нехватке времени.",
        "Но чаще мы откладываем именно те дела, которые для нас важны.",
        "Мозг быстро замечает риск ошибки и включает привычное избегание.",
        "Поэтому простая задача вдруг ощущается тяжелее, чем она есть.",
        "Мы открываем сообщения, ленту или находим срочную мелочь.",
        "Так появляется короткое облегчение, которое закрепляет привычку откладывать.",
        "Работает другой подход: уменьшить первый шаг до смешного размера.",
        "Начни с двух минут, и сопротивление заметно падает.",
        "После старта продолжить обычно гораздо легче, чем начать.",
        "Поэтому цель не закончить всё сразу, а просто войти в действие.",
    ]
    scenes = []
    motions = ["zoom_in", "pan_right", "static", "pan_left", "zoom_out"]
    for index, narration in enumerate(scene_narrations, start=1):
        scenes.append({
            "id": f"s{index}",
            "narration": narration,
            "visual_prompt": f"Vertical cinematic everyday productivity shot number {index}, realistic lighting, clean composition",
            "stock_query": "productivity desk",
            "prefer_stock": True,
            "duration_hint": 3,
            "importance": 3,
            "motion_type": motions[(index - 1) % len(motions)],
            "transition": "cut",
            "reference_scene_id": "",
        })
    return json.dumps({
        "title": "Короткий тестовый ролик",
        "language": "ru",
        "target_duration": 40,
        "narration": "",
        "visual_style": "Реалистичный вертикальный ролик, мягкий естественный свет, живые бытовые детали",
        "scenes": scenes,
    }, ensure_ascii=False)


@pytest.mark.asyncio
async def test_planner_retries_truncated_json_once():
    client = _PlannerClient([
        _planner_response('{ title: truncated', finish_reason="length"),
        _planner_response(_valid_planner_json()),
    ])
    plan = await ScriptPlanner(client=client).plan("Почему люди откладывают дела")
    assert plan.title == "Короткий тестовый ролик"
    assert len(client.calls) == 2
    assert client.calls[0]["max_tokens"] >= 3000
    assert client.calls[0]["response_format"]["type"] == "json_schema"
    assert client.calls[0]["provider"]["require_parameters"] is True
    assert len(plan.scenes) >= 8
    assert plan.narration.startswith("Сначала кажется")


@pytest.mark.asyncio
async def test_planner_fails_cleanly_after_second_invalid_transport_response():
    client = _PlannerClient([
        _planner_response("{ broken", finish_reason="length"),
        _planner_response("not json"),
    ])
    with pytest.raises(RuntimeError, match="valid JSON"):
        await ScriptPlanner(client=client).plan("Тестовая тема")


@pytest.mark.asyncio
async def test_planner_does_not_pay_retry_for_semantic_schema_error():
    broken = json.loads(_valid_planner_json())
    broken["scenes"] = broken["scenes"][:2]
    client = _PlannerClient([_planner_response(json.dumps(broken, ensure_ascii=False))])
    with pytest.raises(RuntimeError, match="structured plan"):
        await ScriptPlanner(client=client).plan("Тестовая тема")
    assert len(client.calls) == 1


def test_video_plan_schema_valid():
    raw = '{"title":"Test","language":"ru","target_duration":35,"narration":"Hello","visual_style":"Modern flat","scenes":[{"id":"s1","narration":"Hello","visual_prompt":"Office","duration_hint":5,"importance":3,"motion_type":"zoom_in","transition":"fade"},{"id":"s2","narration":"Two","visual_prompt":"People","duration_hint":8,"importance":3,"motion_type":"pan_right","transition":"fade"},{"id":"s3","narration":"Done","visual_prompt":"Done","duration_hint":5,"importance":3,"motion_type":"static","transition":"fade"}]}'
    schema = VideoPlanSchema.model_validate_json(raw)
    assert schema.title == "Test"
    assert len(schema.scenes) == 3


def test_video_plan_invalid_too_short_scenes():
    raw = '{"title":"T","language":"ru","target_duration":20,"narration":"x","visual_style":"x","scenes":[{"id":"s1","narration":"a","visual_prompt":"b","duration_hint":5,"importance":3,"motion_type":"zoom_in","transition":"fade"}]}'
    with pytest.raises(Exception):
        VideoPlanSchema.model_validate_json(raw)


def test_video_plan_normalizes_free_form_motion_and_numeric_ids():
    raw = '{"title":"Test","language":"ru","target_duration":20,"narration":"x","visual_style":"Long enough style","scenes":[{"id":1,"narration":"a","visual_prompt":"b","duration_hint":5,"importance":3,"motion_type":"slide","transition":"hard cut","reference_scene_id":null},{"id":2,"narration":"b","visual_prompt":"c","duration_hint":5,"importance":3,"motion_type":"slow push in","transition":"fade","reference_scene_id":1},{"id":3,"narration":"c","visual_prompt":"d","duration_hint":5,"importance":3,"motion_type":"static hold","transition":"fade","reference_scene_id":null}]}'
    schema = VideoPlanSchema.model_validate_json(raw)
    assert schema.scenes[0].id == "1"
    assert schema.scenes[0].motion_type == "pan_right"
    assert schema.scenes[0].transition == "cut"
    assert schema.scenes[1].motion_type == "zoom_in"
    assert schema.scenes[1].reference_scene_id == "1"
    assert schema.scenes[2].motion_type == "static"


def test_video_plan_clamps_noncritical_planner_fields():
    raw = '{"title":"x","language":"ru-RU","target_duration":999,"narration":"x","visual_style":"x","scenes":[{"id":1,"narration":"a","visual_prompt":"b","stock_query":"query","duration_hint":0,"importance":99,"motion_type":"zoom_in","transition":"cut"},{"id":2,"narration":"b","visual_prompt":"c","stock_query":"query","duration_hint":99,"importance":0,"motion_type":"zoom_in","transition":"cut"},{"id":3,"narration":"c","visual_prompt":"d","stock_query":"query","duration_hint":5,"importance":3,"motion_type":"static","transition":"cut"}]}'
    schema = VideoPlanSchema.model_validate_json(raw)
    assert schema.title == "ReCut video"
    assert schema.language == "ru"
    assert schema.target_duration == 120
    assert schema.visual_style == "Realistic vertical short-form video"
    assert schema.scenes[0].duration_hint == 1.0
    assert schema.scenes[0].importance == 5
    assert schema.scenes[1].duration_hint == 30.0
    assert schema.scenes[1].importance == 1


def test_cost_tracker_estimate():
    tracker = CostTracker(
        user_id=1,
        job_id="j1",
        planner_model="anthropic/claude-haiku-5.5",
        planner_input_tokens=500,
        planner_output_tokens=800,
        images_generated=5,
        audio_duration_seconds=40,
    )
    assert tracker.estimated_cost_usd() == 0
    tracker.add_actual_cost("planner", 0.00012)
    tracker.add_actual_cost("image", 0.0004)
    assert abs(tracker.actual_cost_usd - 0.00052) < 1e-10


def test_script_planner_init():
    planner = ScriptPlanner()
    assert planner.model.startswith("anthropic/") or planner.model.startswith("google/")


@pytest.mark.asyncio
async def test_image_generator_stops_on_insufficient_credits_without_fallback():
    class _NoCreditsClient:
        def __init__(self):
            self.models = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def generate_image(self, *, model, **kwargs):
            self.models.append(model)
            error = RuntimeError("payment required")
            error.response = SimpleNamespace(status_code=402)
            raise error

    client = _NoCreditsClient()
    generator = ImageGenerator(
        model="valid/image-model",
        fallback_model="",
        concurrency=1,
        max_retries=2,
        client=client,
    )
    assert generator.fallback_model == ""
    with pytest.raises(ImageCreditsExhaustedError):
        await generator.generate(["scene one", "scene two"])
    assert client.models == ["valid/image-model"]


@pytest.mark.asyncio
async def test_image_generator_reuses_successful_asset_after_partial_credit_failure():
    class _PartiallyFundedClient:
        def __init__(self):
            self.calls = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def generate_image(self, *, model, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return ["https://example.test/first.png"]
            error = RuntimeError("payment required")
            error.response = SimpleNamespace(status_code=402)
            raise error

    client = _PartiallyFundedClient()
    generator = ImageGenerator(
        model="valid/image-model", fallback_model="", concurrency=1, client=client,
    )
    results = await generator.generate(["scene one", "scene two", "scene three"])
    assert results == [
        "https://example.test/first.png",
        "https://example.test/first.png",
        "https://example.test/first.png",
    ]
    assert client.calls == 2


@pytest.mark.asyncio
async def test_tts_falls_back_to_edge_when_openrouter_rejects_request(tmp_path, monkeypatch):
    import httpx
    import app.services.script_video.tts_adapter as tts_module

    class _FailedClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, *args, **kwargs):
            request = httpx.Request("POST", "https://openrouter.ai/api/v1/audio/speech")
            response = httpx.Response(400, request=request, json={"error": {"code": "bad_request"}})
            raise httpx.HTTPStatusError("bad request", request=request, response=response)

    class _FakeCommunicate:
        def __init__(self, text, voice):
            assert text == "Тест озвучки"
            assert voice == "ru-RU-DmitryNeural"

        async def save(self, path):
            from pathlib import Path
            Path(path).write_bytes(b"fixture-audio")

    monkeypatch.setattr(tts_module, "get_settings", lambda: SimpleNamespace(
        script_video_tts_model="openai/gpt-4o-mini-tts-2025-12-15",
        openrouter_api_key="test-key",
    ))
    monkeypatch.setattr(tts_module.httpx, "AsyncClient", lambda **kwargs: _FailedClient())
    monkeypatch.setitem(sys.modules, "edge_tts", SimpleNamespace(Communicate=_FakeCommunicate))

    output = await ScriptTTSAdapter(fallback_model="").synthesize_narration(
        "Тест озвучки", tmp_path / "voice.mp3"
    )
    assert output.read_bytes() == b"fixture-audio"


def _sample_plan():
    scenes = tuple(
        Scene(
            id=f"s{i}", narration=f"Фраза {i}.", visual_prompt="fixture",
            duration_hint=8, importance=3,
            motion_type="zoom_in" if i % 2 else "pan_right",
            transition="fade", reference_scene_id=None,
        )
        for i in range(1, 6)
    )
    return VideoPlan(
        title="Тестовый ролик", language="ru", target_duration=40,
        narration="Тестовая озвучка.", visual_style="Тестовый стиль",
        scenes=scenes,
    )


def test_timeline_preserves_planned_events_without_arbitrary_subcuts():
    plan = _sample_plan()
    beats = TimelineCompiler().compile(plan, image_count=2, audio_duration=40)
    assert len(beats) == len(plan.scenes)
    assert max(beat.duration for beat in beats) > 3.1
    assert {beat.image_index for beat in beats} == {0, 1}
    assert len({beat.motion for beat in beats}) > 1


def test_timeline_duration_matches_audio_after_real_transition_overlap():
    plan = _sample_plan()
    compiler = TimelineCompiler()
    beats = compiler.compile(plan, image_count=5, audio_duration=8.0)
    overlaps = sum(1 for beat in beats[1:] if beat.transition != "cut")
    rendered = sum(beat.duration for beat in beats) - overlaps * compiler.transition_seconds
    assert abs(rendered - 8.0) < 0.05


@pytest.mark.asyncio
async def test_assembler_renders_vertical_mp4_with_audio_and_valid_probe(tmp_path, monkeypatch):
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("FFmpeg and ffprobe are required for the integration test")

    image_paths = []
    for index, color in enumerate(("red", "blue", "green")):
        image_path = tmp_path / f"scene-{index}.png"
        subprocess.run(
            [ffmpeg, "-v", "error", "-f", "lavfi", "-i",
             f"color=c={color}:s=360x640", "-frames:v", "1", str(image_path)],
            check=True, timeout=30,
        )
        image_paths.append(str(image_path))

    audio_path = tmp_path / "voice.wav"
    subprocess.run(
        [ffmpeg, "-v", "error", "-f", "lavfi", "-i",
         "sine=frequency=440:duration=8", "-c:a", "pcm_s16le", str(audio_path)],
        check=True, timeout=30,
    )
    output_path = tmp_path / "result.mp4"
    result = await ScriptVideoAssembler().assemble(
        plan=_sample_plan(), images=image_paths, audio_path=audio_path,
        output_path=output_path,
    )
    probe = subprocess.run(
        [ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(result)],
        check=True, capture_output=True, text=True, timeout=30,
    )
    metadata = json.loads(probe.stdout)
    video = next(stream for stream in metadata["streams"] if stream["codec_type"] == "video")
    audio = next(stream for stream in metadata["streams"] if stream["codec_type"] == "audio")
    assert (video["width"], video["height"]) == (1080, 1920)
    assert video["codec_name"] == "h264"
    assert video["pix_fmt"] == "yuv420p"
    assert audio["codec_name"] == "aac"
    assert int(audio["sample_rate"]) == 48000
    assert abs(float(metadata["format"]["duration"]) - 8.0) < 0.5
    levels = subprocess.run(
        [
            ffmpeg, "-hide_banner", "-i", str(result), "-vn",
            "-af", "volumedetect", "-f", "null", "-",
        ],
        check=True, capture_output=True, text=True, timeout=30,
    ).stderr
    mean_volume = float(next(
        line.rsplit(":", 1)[1].split()[0]
        for line in levels.splitlines()
        if "mean_volume:" in line
    ))
    assert -17.0 < mean_volume < -11.0


@pytest.mark.asyncio
async def test_assembler_rejects_nonempty_invalid_mp4_after_encoder_success(tmp_path, monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock
    from app.services.script_video import assembly

    working_dirs = []
    mkdtemp = assembly.tempfile.mkdtemp

    def make_working_dir(*args, **kwargs):
        path = mkdtemp(*args, **kwargs)
        working_dirs.append(Path(path))
        return path

    monkeypatch.setattr(assembly.tempfile, "mkdtemp", make_working_dir)

    if not shutil.which("ffprobe"):
        pytest.skip("ffprobe is required for the output validation check")
    image_path = tmp_path / "image.png"
    image_path.write_bytes(b"fixture")
    audio_path = tmp_path / "audio.wav"
    audio_path.write_bytes(b"fixture")
    output_path = tmp_path / "output.mp4"
    assembler = ScriptVideoAssembler()
    monkeypatch.setattr(assembler, "audio_duration", AsyncMock(return_value=2.0))
    monkeypatch.setattr(assembler, "_render_beat", AsyncMock())
    create_process = asyncio.create_subprocess_exec

    async def fake_encode(*command, **kwargs):
        if "ffprobe" in str(command[0]):
            return await create_process(*command, **kwargs)
        output_path.write_bytes(b"nonempty but not a valid MP4")
        return SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(b"", b"")))

    monkeypatch.setattr(assembly.asyncio, "create_subprocess_exec", fake_encode)
    with pytest.raises(ScriptVideoAssemblyError, match="ffprobe validation"):
        await assembler.assemble(
            _sample_plan(), [str(image_path)], audio_path=audio_path, output_path=output_path,
        )
    assert working_dirs and all(not path.exists() for path in working_dirs)
    assert image_path.read_bytes() == b"fixture"
    assert audio_path.read_bytes() == b"fixture"
