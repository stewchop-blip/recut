"""ScriptPlanner — generates VideoPlan via OpenRouter structured output."""
from __future__ import annotations

from typing import Optional

from pydantic import ValidationError

from app.core.config import get_settings
from app.core.logging import get_logger
from app.services.script_video.models import VideoPlanSchema, from_schema, VideoPlan
from app.services.script_video.openrouter_client import OpenRouterClient

logger = get_logger(__name__)

SYSTEM_PROMPT = """You are ReCut ScriptPlanner. Create a vertical short-video plan.

OUTPUT FORMAT: one strict JSON object matching VideoPlan schema. No markdown, comments, or text outside JSON.
Fields: title, language, target_duration, narration, visual_style, scenes[].

SCENE rules:
- Use 4-6 scenes. Keep the response compact so it remains valid JSON.
- Each scene must have: id (s1, s2...), narration (spoken text), visual_prompt (descriptive image prompt), duration_hint (seconds), importance (1-5), motion_type (zoom_in/zoom_out/pan_left/pan_right/static), transition (fade/slide_left/slide_right/cut), reference_scene_id (optional, reuse character/object).
- Visual style must describe: main character, setting, lighting, camera, colors, mood.
- Use simple modern language, no jargon, no filler.
- Keep each scene narration to one or two short sentences; visual_prompt to one concise sentence.
- The top-level narration must contain the complete voiceover and be concise enough for the requested duration.
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
        self.actual_cost_usd = 0.0

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
                    "Generate a compact, complete VideoPlan JSON object. Use 4-6 scenes. "
                    "Do not include any extra text."
                ),
            },
        ]
        async with self.client as c:
            schema = None
            for attempt in range(2):
                request_messages = messages if attempt == 0 else [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": (
                            f"Create a new video plan for this idea: {user_idea}\n"
                            f"Duration: {target_duration} seconds. Language: {language}.\n"
                            "The previous output was truncated or invalid. Return exactly one valid JSON object, "
                            "with only 4 scenes, short narration, short visual prompts, and all required fields. "
                            "Do not include markdown or any text outside the JSON object."
                        ),
                    },
                ]
                try:
                    data = await c.chat_completion(
                        model=self.model,
                        messages=request_messages,
                        temperature=0.1,
                        max_tokens=4000,
                        response_format={"type": "json_object"},
                    )
                    usage = data.get("usage") or {}
                    cost = usage.get("cost") if isinstance(usage, dict) else None
                    if isinstance(cost, (int, float)) and cost >= 0:
                        self.actual_cost_usd += float(cost)
                    choice = (data.get("choices") or [{}])[0]
                    content = choice.get("message", {}).get("content", "")
                    if not isinstance(content, str) or not content.strip():
                        raise ValueError("Planner returned empty response")
                    if choice.get("finish_reason") == "length":
                        raise ValueError("Planner response was truncated")
                    content = content.strip()
                    if content.startswith("```json"):
                        content = content[7:]
                    elif content.startswith("```"):
                        content = content[3:]
                    if content.endswith("```"):
                        content = content[:-3]
                    # Some providers wrap JSON in a short preamble despite JSON mode.
                    start, end = content.find("{"), content.rfind("}")
                    if start < 0 or end < start:
                        raise ValueError("Planner returned no JSON object")
                    schema = VideoPlanSchema.model_validate_json(content[start:end + 1])
                    break
                except (ValidationError, ValueError, TypeError) as exc:
                    if attempt == 0:
                        logger.warning(
                            "script_planner_invalid_response_retrying",
                            model=self.model,
                            error_type=type(exc).__name__,
                        )
                    else:
                        logger.error(
                            "script_planner_invalid_response",
                            model=self.model,
                            error_type=type(exc).__name__,
                        )
                        raise RuntimeError("Planner could not return a valid video plan") from exc
            if schema is None:
                raise RuntimeError("Planner could not return a valid video plan")
        plan = from_schema(schema)
        logger.info(
            "script_plan_ready",
            title=plan.title,
            scenes=len(plan.scenes),
            duration_target=plan.target_duration,
        )
        return plan
