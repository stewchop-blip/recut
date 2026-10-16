"""ScriptPlanner - generates VideoPlan via OpenRouter with local validation."""
from __future__ import annotations

import json
from typing import Optional

from pydantic import ValidationError

from app.core.config import get_settings
from app.core.logging import get_logger
from app.services.script_video.models import VideoPlan, VideoPlanSchema, from_schema
from app.services.script_video.openrouter_client import OpenRouterClient

logger = get_logger(__name__)

SYSTEM_PROMPT = """You are ReCut Short-Form Director. Create a production-ready plan for a vertical TikTok/Reels/Shorts video.

Return ONLY one compact JSON object, with no Markdown or commentary.
OpenRouter may enforce the JSON schema when the selected endpoint supports it; ReCut also validates the response locally.

OUTPUT CONTRACT:
- Top-level fields: title, language, target_duration, narration, visual_style, scenes.
- Every scene must contain: id, narration, visual_prompt, stock_query, prefer_stock, duration_hint, importance, motion_type, transition, reference_scene_id.
- motion_type must be one of: zoom_in, zoom_out, pan_left, pan_right, static.
- transition must be one of: fade, slide_left, slide_right, cut.

TOKEN BUDGET IS IMPORTANT:
- Keep the whole JSON compact.
- Set top-level narration to an empty string. ReCut derives it from scenes[].narration.
- visual_style: maximum 35 words.
- Every scene narration: maximum 14 words.
- Every visual_prompt: maximum 28 English words.
- Every stock_query: 2-5 simple English words.

STORY + VOICEOVER rules:
- Write for spoken delivery, not for an article. The voiceover must sound like a real creator talking to one person.
- Open immediately with a hook. No greeting, title announcement, or "today we will" intro.
- For Russian, use natural conversational Russian with short phrases and punctuation that creates human pauses.
- Avoid stiff enumerations unless the idea truly requires a list.
- Aim for roughly 2.0-2.2 spoken words per second so narration sounds natural instead of rushed.

HYBRID VISUAL rules:
- ReCut mixes free real stock video with a small number of AI keyframes. Decide which source fits each scene.
- Set prefer_stock=true for generic real-world B-roll that stock libraries can plausibly contain: hands, streets, desks, phones, laptops, markets, driving, cooking, offices, nature, walking, objects in use, ordinary environments.
- Set prefer_stock=false only for a very specific fictional/story detail, continuity, impossible coincidence, custom composition, or key reveal.
- Across a normal 8-12 scene video, target roughly 65-80% prefer_stock=true.
- stock_query describes only the searchable action/object, not camera jargon.
- visual_prompt is the AI fallback and MUST be English. Describe one concrete photographic/cinematic 9:16 shot.
- Avoid fake interface text, captions baked into images, logos, watermarks, crowds of faces, and generic stock-photo poses.
- If the same AI character/object must recur, set reference_scene_id to that earlier scene id. Otherwise use an empty string.

EDITING rules:
- Think in shots, not slides. Each scene is one distinct visual beat.
- Use cuts for energetic changes, with a few deliberate fades/slides.
- Vary shot scale and angle across adjacent scenes.

QUALITY BAR:
- The result must feel like edited short-form content, not an AI slideshow.
- Every visual must support the spoken idea at that moment.
- Do not invent claims, statistics, brands, UI text, or factual details not supplied by the user.
"""

# Keep the schema intentionally simple. Anthropic structured outputs support the
# enum/type shape reliably, while some numerical JSON-Schema constraints vary by
# provider. Business limits stay enforced by Pydantic after parsing.
VIDEO_PLAN_JSON_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "language": {"type": "string"},
        "target_duration": {"type": "integer"},
        "narration": {"type": "string"},
        "visual_style": {"type": "string"},
        "scenes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "narration": {"type": "string"},
                    "visual_prompt": {"type": "string"},
                    "stock_query": {"type": "string"},
                    "prefer_stock": {"type": "boolean"},
                    "duration_hint": {"type": "number"},
                    "importance": {"type": "integer"},
                    "motion_type": {
                        "type": "string",
                        "enum": ["zoom_in", "zoom_out", "pan_left", "pan_right", "static"],
                    },
                    "transition": {
                        "type": "string",
                        "enum": ["fade", "slide_left", "slide_right", "cut"],
                    },
                    "reference_scene_id": {"type": "string"},
                },
                "required": [
                    "id", "narration", "visual_prompt", "stock_query", "prefer_stock",
                    "duration_hint", "importance", "motion_type", "transition",
                    "reference_scene_id",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": [
        "title", "language", "target_duration", "narration", "visual_style", "scenes"
    ],
    "additionalProperties": False,
}


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

    @staticmethod
    def _extract_json(content: str) -> str:
        content = content.strip()
        if content.startswith("```json"):
            content = content[7:]
        elif content.startswith("```"):
            content = content[3:]
        if content.endswith("```"):
            content = content[:-3]
        start, end = content.find("{"), content.rfind("}")
        if start < 0 or end < start:
            raise ValueError("Planner returned no JSON object")
        return content[start:end + 1]

    @staticmethod
    def _finalize_schema(schema: VideoPlanSchema, target_words: int) -> VideoPlanSchema:
        scene_narration = " ".join(
            scene.narration.strip() for scene in schema.scenes if scene.narration.strip()
        ).strip()
        if not scene_narration:
            raise ValueError("Planner narration is empty")
        # Do not pay for a second model call because the first script is slightly
        # shorter than the target. Audio duration becomes the source of truth.
        spoken_words = len(scene_narration.split())
        if spoken_words < max(24, round(target_words * 0.55)):
            raise ValueError("Planner narration is unusably short")
        if len(schema.scenes) < 6:
            raise ValueError("Planner returned too few visual scenes")
        schema.narration = scene_narration
        for index, scene in enumerate(schema.scenes, start=1):
            if not scene.id.strip():
                scene.id = f"s{index}"
            if not scene.stock_query.strip():
                scene.stock_query = "generic lifestyle"
            scene.duration_hint = max(1.0, min(30.0, len(scene.narration.split()) / 2.1))
        return schema

    async def plan(
        self,
        user_idea: str,
        target_duration: int = 40,
        language: str = "ru",
    ) -> VideoPlan:
        target_words = max(35, round(target_duration * 2.1))
        target_scenes = min(12, max(8, round(target_duration / 4.0)))
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"User idea: {user_idea}\n"
                    f"Target duration: {target_duration} seconds\n"
                    f"Language: {language}\n"
                    f"Target voiceover length: about {target_words} spoken words.\n"
                    f"Use exactly {target_scenes} distinct scenes/shots.\n"
                    "Prefer free real stock footage for generic B-roll and reserve AI imagery for key story-specific shots."
                ),
            },
        ]
        response_format = {
            "type": "json_schema",
            "json_schema": {
                "name": "recut_video_plan",
                "strict": True,
                "schema": VIDEO_PLAN_JSON_SCHEMA,
            },
        }

        async with self.client as c:
            # Keep the caller contract stable for tests and future provider
            # preferences. OpenRouterClient deliberately downgrades the hard
            # router enforcement so regional availability cannot make this
            # request unroutable, then falls back to a portable request on a
            # parameter-routing 404.
            for attempt in range(2):
                try:
                    data = await c.chat_completion(
                        model=self.model,
                        messages=messages,
                        temperature=0.25,
                        max_tokens=3200,
                        response_format=response_format,
                        reasoning={"enabled": False},
                        provider={"require_parameters": True},
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

                    raw_json = self._extract_json(content)
                    # json.loads first differentiates malformed transport output
                    # from semantic validation. Semantic errors must not trigger
                    # another paid attempt.
                    json.loads(raw_json)
                    try:
                        schema = VideoPlanSchema.model_validate_json(raw_json)
                        schema = self._finalize_schema(schema, target_words)
                    except ValidationError as exc:
                        logger.error(
                            "script_planner_schema_rejected_no_retry",
                            model=self.model,
                            reason=str(exc)[:240],
                        )
                        raise RuntimeError("Planner returned an invalid structured plan") from exc
                    except ValueError as exc:
                        logger.error(
                            "script_planner_quality_rejected_no_retry",
                            model=self.model,
                            reason=str(exc)[:240],
                        )
                        raise RuntimeError("Planner returned an unusable plan") from exc
                    plan = from_schema(schema)
                    logger.info(
                        "script_plan_ready",
                        title=plan.title,
                        scenes=len(plan.scenes),
                        stock_preferred=sum(1 for scene in plan.scenes if scene.prefer_stock),
                        duration_target=plan.target_duration,
                        planner_attempts=attempt + 1,
                    )
                    return plan
                except RuntimeError:
                    raise
                except (json.JSONDecodeError, ValueError, TypeError) as exc:
                    if attempt == 0:
                        logger.warning(
                            "script_planner_transport_retrying",
                            model=self.model,
                            error_type=type(exc).__name__,
                            reason=str(exc)[:160],
                        )
                        continue
                    logger.error(
                        "script_planner_transport_failed",
                        model=self.model,
                        error_type=type(exc).__name__,
                        reason=str(exc)[:160],
                    )
                    raise RuntimeError("Planner could not return valid JSON") from exc
        raise RuntimeError("Planner could not return a valid video plan")