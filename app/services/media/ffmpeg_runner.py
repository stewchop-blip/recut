"""FFmpegRunner (Phase 5) — primitive layer adapted from ffmpeg-video-bot
core.py (MIT) + reels-downloader-bot process-tree termination (Apache-2.0).

Responsibilities: spawn ffmpeg, capture stderr, -progress pipe:1 with
out_time_ms parsing, timeout, cancel (kill process tree), structured
FFmpegResult (audit #21-24).
"""
from __future__ import annotations

import asyncio
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from app.core.logging import get_logger

logger = get_logger(__name__)


class FFmpegError(RuntimeError):
    error_code = "FFMPEG_FAILED"


@dataclass(slots=True, frozen=True)
class FFmpegResult:
    success: bool
    return_code: int
    stderr_tail: str
    duration_seconds: float
    output_path: Path | None


@dataclass(slots=True)
class FFmpegProgress:
    out_time_ms: float = 0.0
    frame: int = 0
    fps: float = 0.0
    speed: float = 0.0

    @property
    def percent(self) -> float:
        """Percent requires total duration; caller injects it."""
        return self._percent

    _percent: float = 0.0


class FFmpegRunner:
    """Runs one ffmpeg job; parseable progress; cancel kills the tree."""

    def __init__(self) -> None:
        self._ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
        self._proc: asyncio.subprocess.Process | None = None
        self._cancelled = False

    async def run(self, args: list[str], *, output_path: Path | None = None,
                  total_duration: float = 0.0,
                  on_progress=None,   # callable(FFmpegProgress)
                  timeout_seconds: float = 900.0) -> FFmpegResult:
        started = time.monotonic()
        self._cancelled = False
        # Windows: create_new_process_group lets us kill the whole tree;
        # on POSIX use start_new_session + os.killpg.
        import sys
        kwargs = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = asyncio.subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            import os
            kwargs["preexec_fn"] = getattr(__import__("os"), "setsid", None)
        cmd = [self._ffmpeg, "-y", "-v", "error",
               "-progress", "pipe:1", "-nostats", *args]
        self._proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            **kwargs)
        progress = FFmpegProgress()
        stderr_tail = ""

        async def _pump_stderr() -> None:
            nonlocal stderr_tail
            assert self._proc and self._proc.stderr
            while True:
                line = await self._proc.stderr.readline()
                if not line:
                    break
                stderr_tail = (stderr_tail + line.decode(errors="ignore"))[-2000:]

        async def _pump_stdout() -> None:
            assert self._proc and self._proc.stdout
            while True:
                line = await self._proc.stdout.readline()
                if not line:
                    break
                text = line.decode(errors="ignore").strip()
                if text.startswith("out_time_ms="):
                    try:
                        progress.out_time_ms = float(text.split("=", 1)[1])
                    except ValueError:
                        pass
                elif text.startswith("frame="):
                    try:
                        progress.frame = int(text.split("=", 1)[1])
                    except ValueError:
                        pass
                if total_duration > 0 and progress.out_time_ms > 0:
                    progress._percent = min(100.0, progress.out_time_ms / 1000.0
                                            / max(total_duration, 0.001) * 100.0)
                if on_progress is not None:
                    try:
                        on_progress(progress)
                    except Exception:
                        pass

        stderr_task = asyncio.create_task(_pump_stderr())
        stdout_task = asyncio.create_task(_pump_stdout())
        try:
            await asyncio.wait_for(self._proc.wait(), timeout_seconds)
        except asyncio.TimeoutError as e:
            self.cancel()
            raise FFmpegError("ffmpeg timed out") from e
        finally:
            pass
        await stderr_task
        returncode = self._proc.returncode or 0
        self._proc = None
        ok = (returncode == 0) and not self._cancelled
        if output_path is not None and ok and not output_path.exists():
            ok = False
        return FFmpegResult(
            success=ok,
            return_code=returncode,
            stderr_tail=stderr_tail,
            duration_seconds=time.monotonic() - started,
            output_path=output_path if ok else None,
        )

    def cancel(self) -> None:
        """Kill the whole process tree (audit #24)."""
        self._cancelled = True
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return
        import sys
        try:
            if sys.platform == "win32":
                import subprocess
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                               capture_output=True)
            else:
                import os, signal
                os.killpg(proc.pid, signal.SIGKILL)
        except Exception as e:
            logger.warning("ffmpeg_cancel_failed", error=str(e)[:150])


_runner: FFmpegRunner | None = None


def get_ffmpeg_runner() -> FFmpegRunner:
    global _runner
    if _runner is None:
        _runner = FFmpegRunner()
    return _runner