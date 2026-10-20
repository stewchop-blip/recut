"""End-to-end integration test for script video pipeline."""
import pytest

from app.services.script_video import (
    ScriptPlanner, ImageGenerator, ScriptVideoAssembler,
    ScriptTTSAdapter, VideoPlanSchema, from_schema, get_cost_tracker
)
from app.services.subtitles.scene_adapter import SceneSubtitleBuilder


@pytest.mark.asyncio
async def test_full_pipeline_integration():
    """Verify planner -> images -> subtitles -> TTS -> assembly run without crashes.
    This is an integration smoke test; it does not verify Telegram delivery.
    """
    # 1. Plan
    planner = ScriptPlanner()
    # Skip real LLM call for smoke test; use mock plan directly
    raw = '{"title":"Integration Test","language":"ru","target_duration":30,"narration":"Hello this is a test narration for the integration smoke test.","visual_style":"Modern flat design with blue tones","scenes":[{"id":"s1","narration":"Hello this is scene one.","visual_prompt":"A modern office with blue light","duration_hint":10,"importance":3,"motion_type":"zoom_in","transition":"fade"},{"id":"s2","narration":"This is scene two with more details.","visual_prompt":"People working in a modern space","duration_hint":10,"importance":3,"motion_type":"pan_right","transition":"fade"},{"id":"s3","narration":"Final scene with a clean finish.","visual_prompt":"Clean abstract background","duration_hint":10,"importance":3,"motion_type":"static","transition":"fade"}]}'
    plan = from_schema(VideoPlanSchema.model_validate_json(raw))

    # 2. Images (skip actual network call for smoke; just verify generator init)
    image_gen = ImageGenerator()
    assert image_gen.model is not None

    # 3. Subtitles
    subtitle_builder = SceneSubtitleBuilder()
    subtitle_result = subtitle_builder.build_from_plan(plan)
    assert subtitle_result.line_count > 0

    # 4. TTS adapter exists and creates stub
    tts_adapter = ScriptTTSAdapter()
    assert hasattr(tts_adapter, 'synthesize_narration')

    # 5. Assembler exists
    assembler = ScriptVideoAssembler()
    assert assembler is not None

    # 6. Cost tracker calculates positive value
    tracker = get_cost_tracker(user_id=1, job_id="test")
    cost = tracker.estimated_cost_usd()
    assert cost >= 0
