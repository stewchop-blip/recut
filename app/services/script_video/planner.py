"""ScriptPlanner - generates VideoPlan via OpenRouter structured output."""
from __future__ import annotations

from typing import Optional

from pydantic import ValidationError

from app.core.config import get_settings
from app.core.logging import get_logger
from app.services.script_video.models import VideoPlanSchema, from_schema, VideoPlan
from app.services.script_video.openrouter_client import OpenRouterClient

logger = get_logger(__name__)

SYSTEM_PROMPT = """You are ReCut Short-Form Director. Create a production-ready plan for a vertical TikTok/Reels/Shorts video.

OUTPUT FORMAT: one strict compact JSON object matching VideoPlan schema. No markdown, comments, or text outside JSON.
Fields: title, language, target_duration, narration, visual_style, scenes[].

TOKEN BUDGET IS IMPORTANT:
- Keep the whole JSON compact.
- Set top-level narration to an empty string. ReCut derives it from scenes[].narration.
- visual_style: maximum 35 words.
- Every scene narration: maximum 14 words.
- Every visual_prompt: maximum 28 English words.
- Every stock_query: 2-5 simple English words.
- Never repeat explanations outside the required fields.

STORY + VOICEOVER rules:
- Write for spoken delivery, not for an article. The voiceover must sound like a real creator talking to one person.
- Open immediately with a hook. No greeting, title announcement, or "today we will" intro.
- For Russian, use natural conversational Russian with short phrases and punctuation that creates human pauses.
- Avoid stiff enumerations unless the idea truly requires a list.
- Aim for roughly 2.0-2.2 spoken words per second so narration sounds natural instead of rushed.

HYBRID VISUAL rules:
- ReCut mixes free real stock video with a small number of AI keyframes. Your plan must decide which source fits each scene.
- Each scene must include stock_query and prefer_stock.
- Set prefer_stock=true for generic real-world B-roll that stock libraries can plausibly contain: hands, streets, desks, phones, laptops, markets, driving, cooking, offices, nature, walking, objects in use, ordinary environments.
- Set prefer_stock=false only when the spoken beat needs a very specific fictional/story detail, exact visual continuity, impossible coincidence, custom composition, or a key reveal that generic stock would weaken.
- Across a normal 8-12 scene video, target roughly 65-80% prefer_stock=true and only 20-35% AI-worthy scenes.
- stock_query must describe only the searchable action/object, not camera jargon. Good: "old film camera", "flea market hands", "family beach vacation". Bad: a full cinematic prompt.
- visual_prompt is the AI fallback and MUST be in English. It must describe one concrete photographic/cinematic 9:16 shot.
- Every visual_prompt must be self-contained and include only essential style/character traits needed for continuity.
- Use native vertical composition with important details away from extreme top/bottom UI zones.
- Vary shot scale and angle across adjacent scenes: close-up, medium, wide, POV, over-the-shoulder, detail shot.
- Avoid fake interface text, captions baked into images, logos, watermarks, distorted hands, crowds of faces, and generic stock-photo poses.
- If the same AI character/object must recur, use reference_scene_id and keep age, clothing, setting, lighting, and key traits consistent.

EDITING rules:
- Think in shots, not slides. Each scene is one distinct visual beat.
- Use the requested scene count. For a normal 30-45 second video this is usually 8-12 scenes.
- Each scene must have: id, narration, visual_prompt, stock_query, prefer_stock, duration_hint, importance (1-5), motion_type, transition, reference_scene_id (optional).
- Use cuts for energetic changes, but include a few deliberate fades/slides so the result does not feel mechanically hard-cut throughout.
- For 8-12 scenes, usually 2-4 transitions should be fade/slide and the rest cuts.

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
                    f"Target voiceover length: about {target_words} spoken words (within ±10%).\n"
                    f"Use exactly {target_scenes} distinct scenes/shots.\n"
                    "Prefer free real stock footage for generic B-roll and reserve AI imagery for only the key story-specific shots. "
                    "Return one compact complete VideoPlan JSON object. Set top-level narration to an empty string. "
                    "Keep visual_prompt under 28 words, stock_query under 5 words, and scene narration under 14 words. "
                    "Do not include extra text."
                ),
            },
        ]
        async with self.client as c:
            schema = None
            for attempt in range(2):
                retry_scenes = max(8, target_scenes - 2)
                request_messages = messages if attempt == 0 else [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": (
                            f"Create a new compact hybrid-media video plan for this idea: {user_idea}\n"
                            f"Duration: {target_duration} seconds. Language: {language}.\n"
                            f"Voiceover: about {target_words} spoken words.\n"
                            f"Use exactly {retry_scenes} distinct vertical shots.\n"
                            "The previous output was truncated or invalid. Minimize JSON size aggressively. "
                            "Set top-level narration to an empty string. Keep visual_style under 25 words, "
                            "visual_prompt under 22 words, stock_query under 4 words, and scene narration under 14 words. "
                            "Use stock for most generic scenes and AI only for story-specific keyframes. "
                            "Return exactly one valid JSON object with all required fields and no markdown."
                        ),
                    },
                ]
                try:
                    data = await c.chat_completion(
                        model=self.model,
                        messages=request_messages,
                        temperature=0.3,
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
                    start, end = content.find("{"), content.rfind("}")
                    if start < 0 or end < start:
                        raise ValueError("Planner returned no JSON object")
                    schema = VideoPlanSchema.model_validate_json(content[start:end + 1])

                    scene_narration = " ".join(
                        scene.narration.strip() for scene in schema.scenes if scene.narration.strip()
                    ).strip()
                    spoken_words = len(scene_narration.split())
                    min_words = max(30, round(target_words * 0.80))
                    expected_scenes = target_scenes if attempt == 0 else retry_scenes
                    min_scenes = max(6, expected_scenes - 1)
                    if len(schema.scenes) < min_scenes:
                        raise ValueError("Planner returned too few visual scenes")
                    if spoken_words < min_words:
                        raise ValueError("Planner narration is too short for target duration")
                    schema.narration = scene_narration
                    for scene in schema.scenes:
                        scene.duration_hint = max(1.0, min(30.0, len(scene.narration.split()) / 2.1))
                        if not scene.stock_query.strip():
                            scene.stock_query = "generic lifestyle"
                    break
                except (ValidationError, ValueError, TypeError) as exc:
                    if attempt == 0:
                        logger.warning(
                            "script_planner_invalid_response_retrying",
                            model=self.model,
                            error_type=type(exc).__name__,
                            reason=str(exc)[:160],
                        )
                    else:
                        logger.error(
                            "script_planner_invalid_response",
                            model=self.model,
                            error_type=type(exc).__name__,
                            reason=str(exc)[:160],
                        )
                        raise RuntimeError("Planner could not return a valid video plan") from exc
            if schema is None:
                raise RuntimeError("Planner could not return a valid video plan")
        plan = from_schema(schema)
        logger.info(
            "script_plan_ready",
            title=plan.title,
            scenes=len(plan.scenes),
            stock_preferred=sum(1 for scene in plan.scenes if scene.prefer_stock),
            duration_target=plan.target_duration,
        )
        return plan
