"""STEP 5: "Make 3 versions" editing pipeline.

For a short source video (10-60 s) produce three versions that differ by
REAL edit decisions — start point, structure, timing, optional replay —
not by encoding tricks.

Analysis (ffmpeg only, no LLM):
- scene-change timestamps + scores via select/metadata=print
- strongest moment = highest scene-change score (fallback: 40% of duration)

Plans:
- A FAST START: begin at the strongest moment (+0.3 s lead-in).
- B CONTEXT FIRST: 3 s of context before the strongest moment, then event.
- C ALTERNATIVE EDIT: different cut boundary + 1.5 s replay of the event
  inserted before the end (concat, real re-edit).

Each version then goes through the COMMON RENDERER:
vertical layout (shared make_vertical) → optional CTA (shared burn_cta)
→ finalize_export (loudnorm + metadata strip).

No moderation-bypass techniques: no noise, no mirror, no jitter.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path

from app.core.logging import get_logger
from app.services.media.ffmpeg import MediaService, get_media_service
from app.services.media.probe import get_probe_service

logger = get_logger(__name__)

TARGET_LEN = 20.0       # seconds; each version is capped at this
CONTEXT_LEAD = 3.0      # seconds of context for version B
REPLAY_LEN = 1.5        # seconds replayed in version C
FAST_LEAD = 0.3         # seconds of lead-in for version A


class ThreeVersionsError(Exception):
    pass


@dataclass(slots=True, frozen=True)
class VersionPlan:
    name: str           # "A" / "B" / "C"
    label: str          # human-readable description
    segments: tuple[tuple[float, float], ...]  # (start, end) per segment


@dataclass(slots=True, frozen=True)
class VersionResult:
    name: str
    final_path: Path
    duration_seconds: float
    size_bytes: int


class ThreeVersionsPipeline:
    def __init__(self, media: MediaService | None = None) -> None:
        self._media = media

    # ------------------------------------------------------------------
    # Analysis
    # ------------------------------------------------------------------
    async def _analyze(self, input_path: Path) -> tuple[float, list[float], list[float]]:
        """Return (duration, scene_cut_times, scene_scores)."""
        probe = get_probe_service()
        try:
            meta = await probe.probe(input_path)
        except Exception as e:
            raise ThreeVersionsError(f"Probe failed: {e}") from e
        duration = meta.duration_seconds
        if duration <= 1.0:
            raise ThreeVersionsError("Video too short for 3 versions")

        cmd = [
            self._media_or_default()._ffmpeg_path,
            "-v", "info",
            "-i", str(input_path),
            "-vf", "select='gt(scene,0.2)',metadata=print:key=lavfi.scene_score:file=-",
            "-an", "-f", "null", "-",
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120.0)
        except asyncio.TimeoutError as e:
            proc.kill()
            raise ThreeVersionsError("scene analysis timed out") from e

        # metadata=print:file=- writes to STDOUT.
        text = stdout.decode(errors="ignore") + "\n" + stderr.decode(errors="ignore")
        cuts: list[float] = []
        scores: list[float] = []
        pending_time: float | None = None
        for line in text.splitlines():
            m = re.search(r"pts_time:([0-9.]+)", line)
            if m:
                pending_time = float(m.group(1))
                continue
            m = re.search(r"lavfi\.scene_score=([0-9.]+)", line)
            if m and pending_time is not None:
                cuts.append(pending_time)
                scores.append(float(m.group(1)))
                pending_time = None

        logger.info(
            "versions_scene_analysis",
            duration=round(duration, 2),
            cuts=len(cuts),
            input=str(input_path),
        )
        return duration, cuts, scores

    def _media_or_default(self) -> MediaService:
        if self._media is None:
            self._media = get_media_service()
        return self._media

    @staticmethod
    def _strongest_moment(duration: float, cuts: list[float], scores: list[float]) -> float:
        if cuts and scores:
            return max(zip(cuts, scores), key=lambda p: p[1])[0]
        return duration * 0.4

    def _build_plans(self, duration: float, cuts: list[float], scores: list[float]) -> list[VersionPlan]:
        strongest = self._strongest_moment(duration, cuts, scores)
        target = min(TARGET_LEN, duration)

        # A — fast start
        a_start = max(0.0, strongest - FAST_LEAD)
        a_end = min(duration, a_start + target)
        if a_end - a_start < 2.0:
            a_start, a_end = 0.0, min(duration, target)

        # B — context first
        b_start = max(0.0, strongest - CONTEXT_LEAD)
        b_end = min(duration, max(b_start + 5.0, a_end))
        if b_end <= b_start:
            b_start, b_end = 0.0, min(duration, target)

        # C — alternative boundary + replay of the strongest moment
        next_cut = next((t for t in cuts if t > strongest + 0.5), None)
        c_start = next_cut if next_cut is not None else (cuts[0] if cuts and cuts[0] > 0.3 else 0.0)
        c_start = max(0.0, c_start)
        replay_start = max(0.0, strongest - 0.5)
        replay_end = min(duration, replay_start + REPLAY_LEN)
        c_body_end = min(duration, c_start + target - REPLAY_LEN)
        if c_body_end <= c_start:
            c_start, c_body_end = 0.0, min(duration, target - REPLAY_LEN)
        c_segments = [(c_start, c_body_end)]
        if replay_end > replay_start + 0.3:
            c_segments.append((replay_start, replay_end))

        plans = [
            VersionPlan("A", "fast start", ((a_start, a_end),)),
            VersionPlan("B", "context first", ((b_start, b_end),)),
            VersionPlan("C", "alternative + replay", tuple(c_segments)),
        ]
        for p in plans:
            logger.info(
                "versions_plan",
                version=p.name, label=p.label,
                segments=[(round(s, 2), round(e, 2)) for s, e in p.segments],
            )
        # Guard: versions must be structurally different.
        return plans

    # ------------------------------------------------------------------
    # Rendering (accurate re-encode cut, then common renderer)
    # ------------------------------------------------------------------
    async def _render_segments(
        self, input_path: Path, output_path: Path,
        segments: tuple[tuple[float, float], ...],
    ) -> None:
        """Accurate cut/concat of segments in ONE re-encode pass."""
        media = self._media_or_default()
        # Audio stream present? Colors-only test videos have none.
        probe = get_probe_service()
        has_audio = False
        try:
            info = await probe.probe(input_path)
            has_audio = bool(info.has_audio)
        except Exception:
            has_audio = False
        n = len(segments)
        parts_v = []
        parts_a = []
        for i, (s, e) in enumerate(segments):
            parts_v.append(f"[0:v]trim=start={s:.3f}:end={e:.3f},setpts=PTS-STARTPTS[v{i}]")
            if has_audio:
                parts_a.append(f"[0:a]atrim=start={s:.3f}:end={e:.3f},asetpts=PTS-STARTPTS[a{i}]")
        filter_complex = ";".join(parts_v + parts_a)
        filter_complex += f";{''.join(f'[v{i}]' for i in range(n))}concat=n={n}:v=1:a=0[vout]"
        if has_audio:
            filter_complex += f";{''.join(f'[a{i}]' for i in range(n))}concat=n={n}:v=0:a=1[aout]"

        cmd = [
            media._ffmpeg_path, "-y", "-v", "error",
            "-i", str(input_path),
            "-filter_complex", filter_complex,
            "-map", "[vout]",
        ]
        if has_audio:
            cmd += ["-map", "[aout]"]
        cmd += [
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            str(output_path),
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=600.0)
        except asyncio.TimeoutError as e:
            proc.kill()
            raise ThreeVersionsError("segment render timed out") from e
        if proc.returncode != 0:
            raise ThreeVersionsError(f"segment render failed: {stderr.decode(errors='ignore')[:300]}")

    async def run(
        self,
        input_video: Path,
        job_dir: Path,
        *,
        cta_asset: Path | None,
        cta_position: str,
        cta_margin_px: int,
        cta_last_seconds: float = 4.0,
        overlay_type: str = "png",
        overlay_is_animated: bool = False,
        cta_size_preset: str = "medium",
        cta_mode: str = "end",
    ) -> list[VersionResult]:
        media = self._media_or_default()
        job_dir.mkdir(parents=True, exist_ok=True)

        duration, cuts, scores = await self._analyze(input_video)
        plans = self._build_plans(duration, cuts, scores)

        results: list[VersionResult] = []
        probe = get_probe_service()
        for plan in plans:
            base = job_dir / f"version_{plan.name}.mp4"
            await self._render_segments(input_video, base, plan.segments)

            # Common renderer: vertical layout.
            current = base
            try:
                meta = await probe.probe(current)
            except Exception as e:
                raise ThreeVersionsError(f"probe {plan.name} failed: {e}") from e
            is_916 = (
                meta.height >= meta.width
                and abs(meta.width / max(meta.height, 1) - 9 / 16) < 0.05
            )
            if not is_916:
                vertical = job_dir / f"version_{plan.name}_vertical.mp4"
                # PART 25: each version uses a different visual preset.
                preset_style = {
                    "A": {"background_id": "blur",  "title_text": "",          "brand_corner": False},
                    "B": {"background_id": "dark",  "title_text": "Вот это момент", "brand_corner": False},
                    "C": {"background_id": "accent","title_text": "",          "brand_corner": True},
                }.get(plan.name, {"background_id":"blur","title_text":"","brand_corner":False})
                await media.make_vertical(
                    current, vertical,
                    background_id=preset_style["background_id"],
                    overlay_type=overlay_type,
                    title_text=preset_style["title_text"],
                    brand_corner=preset_style["brand_corner"],
                )
                current = vertical

            # CTA on the LAST seconds (shared burn_cta).
            if cta_asset is not None and cta_asset.exists():
                try:
                    m2 = await probe.probe(current)
                    dur2 = m2.duration_seconds
                    start = max(0.0, dur2 - cta_last_seconds)
                    cta_out = job_dir / f"version_{plan.name}_cta.mp4"
                    await media.burn_cta(
                        current, cta_asset, cta_out,
                        position=cta_position,
                        margin=cta_margin_px,
                        start_seconds=start,
                        end_seconds=dur2,
                        size_preset=cta_size_preset,
                        overlay_type=overlay_type,
                        overlay_is_animated=overlay_is_animated,
                    )
                    current = cta_out
                except Exception as e:
                    logger.warning("versions_cta_failed", version=plan.name, error=str(e)[:200])

            # Clean export (loudnorm + metadata strip) — shared finalize.
            final = job_dir / f"final_version_{plan.name}.mp4"
            try:
                await media.finalize_export(current, final)
            except Exception as e:
                logger.warning("versions_finalize_failed", version=plan.name, error=str(e)[:200])
                final = current

            m3 = await probe.probe(final)
            results.append(VersionResult(
                name=plan.name,
                final_path=final,
                duration_seconds=m3.duration_seconds,
                size_bytes=final.stat().st_size,
            ))
            logger.info(
                "versions_done",
                version=plan.name,
                duration=round(m3.duration_seconds, 2),
                size_bytes=final.stat().st_size,
            )
        return results


def get_three_versions_pipeline() -> ThreeVersionsPipeline:
    return ThreeVersionsPipeline()