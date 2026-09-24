"""AudioProcessor (PART 20-21) — one audio layer for every pipeline.

AudioConfig describes WHAT to do; AudioProcessor builds the ffmpeg
-audio filter chain. Business code never hand-writes loudnorm/EQ again.

Presets (PART 21):
    original  — loudnorm only (input untouched otherwise)
    dynamic   — mild EQ + loudnorm + light compression
    music     — original lowered, music asset mixed on top
    none      — original track removed (music/voice layer only)
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

from app.core.logging import get_logger

logger = get_logger(__name__)

# loudnorm targets (EBU R128-ish, social platforms)
_LN = "loudnorm=I=-14:TP=-1.5:LRA=11"
# mild presence EQ: small highs lift, tiny low cut
_EQ_DYNAMIC = (
    "highpass=f=60,equalizer=f=3000:t=q:w=1:g=1.5,"
    "equalizer=f=200:t=q:w=1:g=-1.0"
)


@dataclass
class AudioConfig:
    preset: str = "original"          # original|dynamic|music|none
    original_volume: float = 1.0      # 0.0..1.5
    music_asset: Path | None = None
    music_volume: float = 0.25
    voice_asset: Path | None = None   # TODO: voiceover layer
    voice_volume: float = 1.0
    fade_in: float = 0.0              # seconds
    fade_out: float = 0.0             # seconds
    speed: float = 1.0                # 0.5..2.0, keeps pitch (atempo)
    normalize: bool = True

    def resolved_preset(self) -> str:
        return self.preset if self.preset in ("original", "dynamic", "music", "none") else "original"


@dataclass
class AudioPlan:
    """Result of planning: input indexes for extra assets + filter chain."""
    filter_chain: str                 # without [a] labels — applied to 0:a
    extra_inputs: list = field(default_factory=list)
    drop_original: bool = False


class AudioProcessor:
    """Builds and runs the audio part of an ffmpeg encode."""

    def plan(self, cfg: AudioConfig) -> AudioPlan:
        preset = cfg.resolved_preset()
        extra: list = []
        chain: list[str] = []

        if preset == "none":
            plan = AudioPlan(filter_chain="", extra_inputs=[], drop_original=True)
            return plan

        vol = f"volume={cfg.original_volume}" if cfg.original_volume != 1.0 else ""
        if preset == "dynamic":
            chain += [_EQ_DYNAMIC, _LN]
        elif preset == "music":
            chain += [_LN]
            chain.append(f"volume={max(0.0, cfg.original_volume * 0.5)}")
        else:  # original
            if vol:
                chain.append(vol)
            if cfg.normalize:
                chain.append(_LN)

        if cfg.speed != 1.0:
            # atempo range is 0.5..2.0; keep it sane
            s = min(2.0, max(0.5, cfg.speed))
            chain.append(f"atempo={s}")

        if cfg.fade_in > 0:
            chain.append(f"afade=t=in:st=0:d={cfg.fade_in}")
        if cfg.fade_out > 0:
            # fade-out length applied from the end is ffmpeg-side tricky
            # without duration; callers pass st= computed when known.
            chain.append(f"afade=t=out:d={cfg.fade_out}")

        if preset == "music" and cfg.music_asset and Path(cfg.music_asset).exists():
            extra += ["-i", str(cfg.music_asset)]
            music_idx = 1
            chain = [  # amix original(quieter) + music looped
                f"[0:a]{','.join(chain)}[a0]",
                f"[{music_idx}:a]volume={cfg.music_volume},aloop=loop=-1:size=2e+09[am]",
                "[a0][am]amix=inputs=2:duration=first:dropout_transition=2[aout]",
            ]
            return AudioPlan(filter_chain=";".join(chain), extra_inputs=extra)

        f = ",".join(chain) if chain else "anull"
        return AudioPlan(filter_chain=f"[0:a]{f}[aout]", extra_inputs=extra)

    def audio_args(self, cfg: AudioConfig, audio_bitrate: str = "128k") -> tuple[list, str | None]:
        """Returns (extra_input_args, map_label). map label None → no audio."""
        plan = self.plan(cfg)
        if plan.drop_original:
            return ([], None)
        args = list(plan.extra_inputs)
        if plan.filter_chain.startswith("[0:a]") and ";" in plan.filter_chain:
            args += ["-filter_complex", plan.filter_chain, "-map", "[aout]"]
        else:
            args += ["-af", plan.filter_chain.removeprefix("[0:a]").removesuffix("[aout]")]
        return (args, "[aout]" if ";" in plan.filter_chain else "0:a?")

    async def process(self, input_path: Path, output_path: Path,
                      cfg: AudioConfig, timeout_seconds: float = 600.0) -> Path:
        """Standalone audio transform (copy video, transform audio)."""
        args, audio_map = self.audio_args(cfg)
        cmd = [self._ffmpeg(), "-y", "-v", "error", "-i", str(input_path)]
        cmd += args
        if audio_map:
            cmd += ["-map", "0:v", "-map", audio_map]
        else:
            cmd += ["-map", "0:v", "-an"]
        cmd += ["-c:v", "copy", "-c:a", "aac", "-b:a", "128k", str(output_path)]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout_seconds)
        if proc.returncode != 0:
            raise RuntimeError(f"AudioProcessor failed: {stderr.decode(errors='ignore')[:300]}")
        return output_path

    @staticmethod
    def _ffmpeg() -> str:
        import shutil
        from app.core.config import get_settings
        try:
            return get_settings().ffmpeg_path
        except Exception:
            return shutil.which("ffmpeg") or "ffmpeg"


# ---- UX presets (PART 21/24): names shown to the user ------------------
AUDIO_PRESETS = {
    "original": {"label": "🎧 Оригинал", "config": {}},
    "dynamic": {"label": "⚡ Динамичный", "config": {"preset": "dynamic"}},
    "music": {"label": "🎵 С музыкой", "config": {"preset": "music", "original_volume": 0.6}},
    "none": {"label": "🔇 Без оригинала", "config": {"preset": "none"}},
}
