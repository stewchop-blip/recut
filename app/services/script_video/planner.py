"""ScriptPlanner — generates VideoPlan via OpenRouter structured output."""
from __future__ import annotations

import json
from typing import Optional

from app.core.config import get_settings
from app.core.logging import get_logger
from app.services.script_video.models import VideoPlanSchema, from_schema, VideoPlan
from app.services.script_video.openrouter_client import OpenRouterClient

logger = get_logger(__name__)

SYSTEM_PROMPT = """You are ReCut ScriptPlanner. Create a 30-60 second vertical video script.

OUTPUT FORMAT: strict JSON matching VideoPlan schema.
Fields: title, language, target_duration, narration, visual_style, scenes[].

SCENE rules:
- 6-10 scenes for 30-60s video.
- Each scene must have: id (s1, s2...), narration (spoken text), visual_prompt (descriptive image prompt), duration_hint (seconds), importance (1-5), motion_type (zoom_in/zoom_out/pan_left/pan_right/static), transition (fade/slide_left/slide_right/cut), reference_scene_id (optional, reuse character/object).
- Visual style must describe: main character, setting, lighting, camera, colors, mood.
- Use simple modern language, no jargon, no filler.
- Keep narration concise — about 2-4 sentences per scene for 30-60s total.
"""


class ScriptPlanner:
    def __init__(
        self,
        model: Optional[str] = None,
        client: Optional[OpenRouterClient] = None,
    ) -> None:
        s = get_settings()
        self.model = model or s.script_video_planner_model
        self.client = client or OpenRouterClient()

    async def plan(
        self,
        user_idea: str,
        target_duration: int = 40,
        language: str = "ru",
    ) -> VideoPlan:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"User idea: {user_idea}\n"
                    f"Target duration: {target_duration} seconds\n"
                    f"Language: {language}\n"
                    "Generate VideoPlan JSON. Do not include any extra text."
                ),
            },
        ]
        async with self.client as c:
            data = await c.chat_completion(
                model=self.model,
                messages=messages,
                temperature=0.2,
                max_tokens=2500,
                response_format={"type": "json_object"},
            )
        content_text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        if not content_text:
            logger.error("script_planner_empty_response", model=self.model)
            raise RuntimeError("Planner returned empty response")
        # Validate
        schema = VideoPlanSchema.model_validate_json(content_text)
        plan = from_schema(schema)
        logger.info(
            "script_plan_ready",
            title=plan.title,
            scenes=len(plan.scenes),
            duration_target=plan.target_duration,
        )
        return plan
