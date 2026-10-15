"""Tests for script video generation pipeline."""
import pytest
from types import SimpleNamespace

from app.services.script_video import VideoPlanSchema, from_schema, CostTracker, ScriptPlanner
from app.services.script_video.image_generator import ImageGenerator, ImageCreditsExhaustedError


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
    return (
        '{"title":"Короткий тестовый ролик","language":"ru","target_duration":30,'
        '"narration":"Краткий текст озвучки.","visual_style":"Современный стиль, мягкий свет",'
        '"scenes":['
        '{"id":"s1","narration":"Начало.","visual_prompt":"Тихий город на рассвете",'
        '"duration_hint":10,"importance":3,"motion_type":"zoom_in","transition":"fade"},'
        '{"id":"s2","narration":"Продолжение.","visual_prompt":"Человек идёт по улице",'
        '"duration_hint":10,"importance":3,"motion_type":"pan_right","transition":"fade"},'
        '{"id":"s3","narration":"Финал.","visual_prompt":"Солнечный горизонт",'
        '"duration_hint":10,"importance":3,"motion_type":"static","transition":"cut"}'
        ']}'
    )


@pytest.mark.asyncio
async def test_planner_retries_truncated_json_once():
    client = _PlannerClient([
        _planner_response('{ title: truncated', finish_reason="length"),
        _planner_response(_valid_planner_json()),
    ])
    plan = await ScriptPlanner(client=client).plan("Почему люди откладывают дела")
    assert plan.title == "Короткий тестовый ролик"
    assert len(client.calls) == 2
    assert client.calls[0]["max_tokens"] >= 4000


@pytest.mark.asyncio
async def test_planner_fails_cleanly_after_second_invalid_response():
    client = _PlannerClient([
        _planner_response("{ broken", finish_reason="length"),
        _planner_response("not json"),
    ])
    with pytest.raises(RuntimeError, match="valid video plan"):
        await ScriptPlanner(client=client).plan("Тестовая тема")


def test_video_plan_schema_valid():
    import json
    raw = '{"title":"Test","language":"ru","target_duration":35,"narration":"Hello","visual_style":"Modern flat","scenes":[{"id":"s1","narration":"Hello","visual_prompt":"Office","duration_hint":5,"importance":3,"motion_type":"zoom_in","transition":"fade"},{"id":"s2","narration":"Two","visual_prompt":"People","duration_hint":8,"importance":3,"motion_type":"pan_right","transition":"fade"},{"id":"s3","narration":"Done","visual_prompt":"Done","duration_hint":5,"importance":3,"motion_type":"static","transition":"fade"}]}'
    schema = VideoPlanSchema.model_validate_json(raw)
    assert schema.title == "Test"
    assert len(schema.scenes) == 3


def test_video_plan_invalid_too_short_scenes():
    import json
    raw = '{"title":"T","language":"ru","target_duration":20,"narration":"x","visual_style":"x","scenes":[{"id":"s1","narration":"a","visual_prompt":"b","duration_hint":5,"importance":3,"motion_type":"zoom_in","transition":"fade"}]}'
    with pytest.raises(Exception):
        VideoPlanSchema.model_validate_json(raw)


def test_video_plan_invalid_motion_type():
    import json
    raw = '{"title":"T","language":"ru","target_duration":20,"narration":"x","visual_style":"x","scenes":[{"id":"s1","narration":"a","visual_prompt":"b","duration_hint":5,"importance":3,"motion_type":"invalid","transition":"fade"},{"id":"s2","narration":"b","visual_prompt":"c","duration_hint":5,"importance":3,"motion_type":"zoom_in","transition":"fade"},{"id":"s3","narration":"c","visual_prompt":"d","duration_hint":5,"importance":3,"motion_type":"static","transition":"fade"}]}'
    with pytest.raises(Exception):
        VideoPlanSchema.model_validate_json(raw)


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
    cost = tracker.estimated_cost_usd()
    assert cost > 0
    assert cost < 0.01


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
        fallback_model="openrouter/auto",
        concurrency=1,
        max_retries=2,
        client=client,
    )
    with pytest.raises(ImageCreditsExhaustedError):
        await generator.generate(["scene one", "scene two"])
    assert client.models == ["valid/image-model"]
