"""Tests for script video generation pipeline."""
import pytest

from app.services.script_video import VideoPlanSchema, from_schema, CostTracker, ScriptPlanner


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
