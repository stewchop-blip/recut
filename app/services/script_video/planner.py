"""ScriptPlanner — generates VideoPlan via OpenRouter structured output."""
from __future__ import annotations

from typing import Optional

from pydantic import ValidationError

from app.core.config import get_settings
from app.core.logging import get_logger
from app.services.script_video.models import VideoPlanSchema, from_schema, VideoPlan
from app.services.script_video.openrouter_client import OpenRouterClient

logger = get_logger(__name__)

SYSTEM_PROMPT = """You are ReCut Short-Form Director. Create a production-ready plan for a vertical TikTok/Reels/Shorts video.

OUTPUT FORMAT: one strict JSON object matching VideoPlan schema. No markdown, comments, or text outside JSON.
Fields: title, language, target_duration, narration, visual_style, scenes[].

STORY + VOICEOVER rules:
- Write for spoken delivery, not for an article. The voiceover must sound like a real creator talking to one person.
- Open immediately with a hook. No greeting, no title announcement, no "today we will" intro.
- For Russian, use natural conversational Russian: short phrases, normal word order, contractions where appropriate, and punctuation that creates human pauses.
- Avoid stiff enumerations like "во-первых / во-вторых / в-третьих" unless the idea truly requires a list.
- Aim for roughly 2.2-2.5 spoken words per second so the narration actually fills the requested duration.
- The top-level narration MUST equal the exact concatenation of scenes[].narration in scene order, separated only by spaces. Do not paraphrase or add words at top level.

VISUAL DIRECTION rules:
- Think in shots, not slides. Use roughly one distinct shot every 2.5-3.5 seconds.
- Use 8-14 scenes for a normal 30-45 second video. Every scene should justify a new visual.
- Each scene must have: id (s1, s2...), narration, visual_prompt, duration_hint, importance (1-5), motion_type (zoom_in/zoom_out/pan_left/pan_right/static), transition (fade/slide_left/slide_right/cut), reference_scene_id (optional).
- visual_prompt MUST be in English even when narration is Russian. It must describe one concrete photographic/cinematic 9:16 shot, not an abstract concept.
- Every visual_prompt must be self-contained and repeat the essential global style/character traits needed for continuity because shots are generated independently.
- Write prompts for native vertical composition: subject framed for 9:16, important details away from extreme top/bottom UI zones.
- Prefer believable creator/B-roll imagery: hands, devices, environments, over-the-shoulder shots, close-ups, details, real-world action, screens viewed at an angle, objects in use.
- Vary shot scale and angle across adjacent scenes: close-up, medium, wide, POV, over-the-shoulder, detail shot. Do not repeat the same "person holding a phone" composition.
- Avoid fake interface text, captions baked into images, logos, watermarks, distorted hands, crowds of faces, and generic stock-photo poses.
- If the same person/object must recur, use reference_scene_id and keep age, clothing, setting, lighting, and key visual traits consistent.
- visual_style must define a coherent look for the whole video: realism level, palette, lighting, lens/camera feel, texture, mood, and recurring character traits if any.
- Prefer hard cuts for energetic short-form pacing. Use a fade/slide only when it serves a deliberate beat.
- Keep each scene narration to one short sentence and each visual_prompt to one concise but specific sentence.

QUALITY BAR:
- The result must feel like edited short-form content, not an AI slideshow.
- Every visual should support the exact spoken idea at that moment.
- Do not add claims, statistics, brands, UI text, or factual details that were not supplied by the user unless they are generic and safe.
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
        target_words = max(35, round(target_duration * 2.35))
        target_scenes = min(14, max(8, round(target_duration / 3.0)))
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"User idea: {user_idea}\n"
                    f"Target duration: {target_duration} seconds\n"
                    f"Language: {language}\n"
                    f"Target voiceover length: about {target_words} spoken words (within ±10%).\n"
                    f"Target shot count: about {target_scenes} distinct scenes/shots.\n"
                    "Generate one compact, complete VideoPlan JSON object. "
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
                            f"Voiceover: about {target_words} spoken words.\n"
                            f"Use about {target_scenes} distinct vertical shots.\n"
                            "The previous output was truncated or invalid. Return exactly one valid compact JSON object, "
                            "with short scene narration and concise visual prompts, while keeping all required fields. "
                            "Do not include markdown or any text outside the JSON object."
                        ),
                    },
                ]
                try:
                    data = await c.chat_completion(
                        model=self.model,
                        messages=request_messages,
                        temperature=0.35,
                        max_tokens=5500,
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
