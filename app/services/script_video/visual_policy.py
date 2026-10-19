"""Conservative visual relevance rules for Script Video.

Text-only searches are not proof that a clip depicts a specific story event.
Fail closed: reject uncertain assets and spend the existing image budget instead.
"""
from __future__ import annotations

import re

_STORY_MARKERS = re.compile(
    r"(?:истори[яюией]|историй|рассказ|сюжет|сказк|мистик|триллер|"
    r"страшил|страшн(?:ую|ой|ая)|трогател|интриг|придуманн|"
    r"fiction(?:al)?|story|mystery|horror|thriller|narrative|tale)",
    re.IGNORECASE,
)

_SEARCH_NOISE = {
    "a", "an", "and", "the", "of", "in", "on", "at", "to", "for", "from",
    "with", "video", "stock", "footage", "clip", "shot", "portrait",
    "cinematic", "vertical", "close", "up", "slow", "motion", "people",
    "person", "generic", "scene", "view", "hd", "4k", "mp4",
}


def is_narrative_request(user_idea: str) -> bool:
    """Treat explicit storytelling prompts as continuity-critical.

    This intentionally does not try to classify every genre. False negatives
    still pass through strict metadata checks. False positives simply avoid
    uncertain stock and keep the same capped image budget.
    """
    return bool(_STORY_MARKERS.search(user_idea or ""))


def _terms(value: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", (value or "").lower())
    return {w for w in words if len(w) > 2 and w not in _SEARCH_NOISE}


def stock_title_matches_query(query: str, title: str) -> bool:
    """Only approve publicly described stock with a strong subject match.

    Search ranking or 'first hit' alone admitted astronauts and broadcast news
    into a fictional camera story. A title is not image verification, but this
    blocks obviously unrelated footage without another paid AI request.
    When metadata lacks enough detail, return False.
    """
    target = _terms(query)
    candidate = _terms(title)
    if len(target) < 2 or not candidate:
        return False
    matched = len(target.intersection(candidate))
    required = len(target) if len(target) <= 2 else max(2, (len(target) * 2 + 2) // 3)
    return matched >= required


def narrative_plan_requires_visual_continuity(user_idea: str, plan: object) -> bool:
    """Detect stories without relying only on the user's exact phrasing."""
    if is_narrative_request(user_idea):
        return True
    # Continuity references are a strong clue that apparently generic stock
    # (old camera, beach, house) will substitute a wrong character or setting.
    scenes = tuple(getattr(plan, "scenes", ()) or ())
    references = sum(bool(getattr(scene, "reference_scene_id", None)) for scene in scenes)
    return references >= 2
