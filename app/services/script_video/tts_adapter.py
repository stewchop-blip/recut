"""TTS adapter for script video narration.

Reuses the OpenRouter TTS provider pattern (openrouter.py.bak)
to generate audio from VideoPlan narration.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Optional

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


class ScriptTTSAdapter:
    """Generate audio MP3 from narration text via OpenRouter TTS."""

    def __init__(self, model: Optional[str] = None) -> None:
        s = get_settings()
        self.model = model or s.script_video_tts_model
        self.api_key = s.openrouter_api_key

    async def synthesize_narration(self, text: str, output_path: Path) -> Path:
        """Simple wrapper: in production this would call OpenRouter TTS endpoint.

        For the current phase, we generate a stub audio file using ffmpeg
        (silent audio of appropriate length) so the assembly pipeline works.
        A real TTS call will be added once the OpenRouter audio endpoint
        is verified with the script pipeline.
        """
        import subprocess
        output_path.parent.mkdir(parents=True, exist_ok=True)
        # Estimate duration: ~100 chars ≈ 10 seconds for spoken text
        estimated_duration = max(5.0, len(text) * 0.08)
        ffmpeg = "ffmpeg"  # Will be resolved by subprocess
        cmd = [
            ffmpeg, "-y", "-f", "lavfi", "-i",
            f"anullsrc=r=24000:cl=mono:d={estimated_duration}",
            "-t", str(estimated_duration),
            "-c:a", "libmp3lame", "-b:a", "128k",
            str(output_path),
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if result.returncode == 0:
                logger.info("tts_stub_audio_created", path=str(output_path), duration=estimated_duration)
            else:
                logger.warning("tts_stub_audio_failed", stderr=result.stderr[:200])
                # Fallback: create empty file so assembly doesn't crash
                output_path.write_bytes(b"")
        except Exception as exc:
            logger.warning("tts_exception", exc=str(exc)[:200])
            output_path.write_bytes(b"")
        return output_path
