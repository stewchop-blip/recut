"""Optional local HyperFrames motion graphic renderer for explainer scenes.

Reuses existing narration, with NO model or remote-media requests. Only trusted,
fixed HTML/CSS is produced. It never executes AI-generated HTML or JS.
Disabled by default and ALWAYS falls back to the existing video pipeline.
"""
from __future__ import annotations

import asyncio
import html
import os
import shutil
import signal
from pathlib import Path

from app.core.logging import get_logger

logger = get_logger(__name__)

MAX_SCENES = 2
MAX_DURATION = 5.0
MAX_OUTPUT_BYTES = 20 * 1024 * 1024


class HyperframesGraphicRenderer:
    """Fail-closed, bounded subprocess for short explanatory motion graphics."""

    def __init__(self, binary: str | None = None, *, enabled: bool | None = None) -> None:
        raw = os.getenv("SCRIPT_VIDEO_HYPERFRAMES_ENABLED", "false").lower()
        self.enabled = (
            enabled if enabled is not None else raw in {"1", "true", "yes", "on"}
        )
        requested = binary or os.getenv("HYPERFRAMES_BIN", "hyperframes")
        self.binary = shutil.which(requested)
        self.timeout_seconds = max(
            10, min(120, self._integer_env("SCRIPT_VIDEO_HYPERFRAMES_TIMEOUT_SECONDS", 75))
        )

    @staticmethod
    def _integer_env(key: str, default: int) -> int:
        try:
            return int(os.getenv(key, str(default)))
        except (ValueError, TypeError):
            return default

    @property
    def available(self) -> bool:
        return self.enabled and self.binary is not None

    @staticmethod
    def selected_indices(scenes: tuple | list) -> tuple[int, ...]:
        """Reserve one or two explanatory beats; do not change story scenes."""
        count = len(scenes)
        if count < 5:
            return ()
        # Deliberately avoid the opening hook and final conclusion.
        candidates = (max(1, count // 3), max(2, (count * 2) // 3))
        return tuple(
            i for i in dict.fromkeys(candidates)
            if i < count - 1 and bool(getattr(scenes[i], "prefer_stock", False))
        )[:MAX_SCENES]

    @staticmethod
    def composition_html(*, title: str, narration: str, duration: float) -> str:
        """Trusted code template with escaped text, no external fonts/scripts.

        HyperFrames reads data-composition-id, data-start, data-duration and
        clip timing. Paused CSS keyframes are seekable by its browser runtime.
        """
        duration = min(MAX_DURATION, max(1.0, float(duration)))
        title_text = html.escape(" ".join(title.split())[:48], quote=True)
        # Only use existing narration, no factual claims or hallucinated stats.
        words = narration.split()
        lead_text = html.escape(" ".join(words[:7])[:76], quote=True)
        tail_text = html.escape(" ".join(words[7:])[:94], quote=True)
        duration_attr = f"{duration:.3f}"
        return (
            '<!doctype html>\n<html lang="ru">\n<head>\n'
            '<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=540,height=960">\n'
            '<style>\n'
            '*{box-sizing:border-box}html,body{width:540px;height:960px;margin:0;'
            'overflow:hidden;background:#111827;color:#f9fafb;font-family:'
            'Arial,DejaVu Sans,sans-serif}\n'
            '#scene{position:relative;width:540px;height:960px;overflow:hidden;'
            'background:linear-gradient(150deg,#111827 0%,#172135 65%,#25233c 100%)}\n'
            '.grid{position:absolute;inset:0;opacity:.15;background-image:'
            'linear-gradient(#8493b333 1px,transparent 1px),'
            'linear-gradient(90deg,#8493b333 1px,transparent 1px);'
            'background-size:46px 46px;transform:rotate(-9deg) scale(1.4)}\n'
            '.orb{position:absolute;top:100px;right:-115px;width:370px;height:370px;'
            'border:3px solid #60a5fa;border-radius:100%;opacity:.5;'
            'animation:orb 5s ease-in-out both paused}\n'
            '.rail{position:absolute;top:145px;left:54px;width:94px;height:7px;'
            'background:#73b6fc;animation:rail .7s ease-out both paused}\n'
            '.heading{position:absolute;top:176px;left:54px;width:425px;'
            'font-size:22px;line-height:1.3;letter-spacing:2px;opacity:.75;'
            'overflow-wrap:break-word}\n'
            '.lead{position:absolute;top:300px;left:50px;width:440px;'
            'font-size:54px;font-weight:800;letter-spacing:-2px;line-height:1.05;'
            'overflow-wrap:break-word;animation:lead .65s ease-out both paused}\n'
            '.tail{position:absolute;top:610px;left:54px;width:432px;'
            'font-size:30px;line-height:1.24;opacity:.86;overflow-wrap:break-word;'
            'animation:tail .7s ease-out both paused;animation-delay:.45s}\n'
            '.line{position:absolute;left:54px;bottom:170px;width:410px;height:3px;'
            'background:#64748b}.fill{height:100%;width:100%;background:#60a5fa;'
            'transform-origin:left;animation:fill '
            + duration_attr + 's linear both paused}\n'
            '@keyframes lead{from{opacity:0;transform:translateY(34px) scale(.97)}'
            'to{opacity:1;transform:translateY(0) scale(1)}}\n'
            '@keyframes tail{from{opacity:0;transform:translateY(22px)}'
            'to{opacity:.86;transform:translateY(0)}}\n'
            '@keyframes rail{from{transform:scaleX(0)}to{transform:scaleX(1)}}\n'
            '@keyframes orb{from{transform:scale(.85) translateY(25px)}'
            'to{transform:scale(1.04) translateY(-18px)}}\n'
            '@keyframes fill{from{transform:scaleX(0)}to{transform:scaleX(1)}}\n'
            '</style>\n</head>\n<body>\n'
            '<div id="scene" data-composition-id="recut-graphic" data-width="540" '
            'data-height="960" data-fps="24" data-no-timeline data-start="0" data-duration="'
            + duration_attr + '">\n'
            '<div class="grid"></div><div class="orb"></div>\n'
            '<div class="rail"></div><div class="heading">'
            + title_text + '</div>\n'
            '<div class="clip lead" data-start="0" data-duration="'
            + duration_attr + '" data-track-index="0">' + lead_text + '</div>\n'
            '<div class="clip tail" data-start="0" data-duration="'
            + duration_attr + '" data-track-index="1">' + tail_text + '</div>\n'
            '<div class="line"><div class="fill"></div></div>\n'
            '</div>\n</body></html>\n'
        )

    @staticmethod
    async def _kill_process_group(proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        await proc.communicate()

    async def render_scene(
        self,
        *,
        title: str,
        narration: str,
        duration: float,
        workdir: Path,
        index: int,
    ) -> Path | None:
        """Return locally rendered MP4, or None for ordinary FFmpeg fallback."""
        if not self.available or len(narration.strip()) < 15:
            return None
        if not 1.0 <= duration <= MAX_DURATION:
            return None
        workdir = workdir.resolve()
        html_path = workdir / f"hyperframes-{index:02d}.html"
        output_path = workdir / f"hyperframes-{index:02d}.mp4"
        html_path.write_text(
            self.composition_html(title=title, narration=narration, duration=duration),
            encoding="utf-8",
        )
        # No Telegram/OpenRouter credentials or user-controlled executable code
        # is forwarded to the renderer. It gets only PATH and a temporary HOME.
        safe_env = {
            "PATH": os.getenv("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": str(workdir),
            "LANG": "C.UTF-8",
            "HYPERFRAMES_NO_UPDATE_CHECK": "1",
            "HYPERFRAMES_NO_TELEMETRY": "1",
            "DO_NOT_TRACK": "1",
            "PRODUCER_LOW_MEMORY_MODE": "1",
            "NO_COLOR": "1",
            # Use a system-installed Chromium from the optional image rather
            # than a browser cache under a temporary HOME.
            "PUPPETEER_EXECUTABLE_PATH": (
                os.getenv("PUPPETEER_EXECUTABLE_PATH")
                or shutil.which("chromium")
                or shutil.which("chromium-browser")
                or ""
            ),
        }
        cmd = [
            self.binary, "render", "-c", str(html_path),
            "--output", str(output_path), "--fps", "24",
            "--workers", "1", "--no-browser-gpu",
            "--low-memory-mode", "--no-best-effort", "--quiet",
            "--quality", "standard",
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=str(workdir), env=safe_env, start_new_session=True,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, err = await asyncio.wait_for(
                    proc.communicate(), timeout=self.timeout_seconds
                )
            except (asyncio.CancelledError, asyncio.TimeoutError):
                await self._kill_process_group(proc)
                raise
            if proc.returncode != 0:
                logger.warning(
                    "script_hyperframes_render_failed",
                    returncode=proc.returncode,
                    error_type="HyperframesExit",
                    detail=(err or b"")[-350:].decode(errors="replace"),
                )
                return None
            if (
                not output_path.is_file()
                or output_path.stat().st_size <= 1024
                or output_path.stat().st_size > MAX_OUTPUT_BYTES
            ):
                logger.warning("script_hyperframes_invalid_output", index=index)
                return None
            logger.info(
                "script_hyperframes_scene_ready",
                index=index, duration=duration,
                output_bytes=output_path.stat().st_size,
            )
            return output_path
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "script_hyperframes_unavailable",
                index=index, error_type=type(exc).__name__,
            )
            return None
        finally:
            html_path.unlink(missing_ok=True)
            if output_path.exists() and (
                output_path.stat().st_size == 0
                or output_path.stat().st_size > MAX_OUTPUT_BYTES
            ):
                output_path.unlink(missing_ok=True)
