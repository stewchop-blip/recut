"""Assemble generated stills, narration, and ASS subtitles into vertical MP4."""
from __future__ import annotations

import asyncio
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Optional

from app.core.logging import get_logger
from app.services.media.ffmpeg import _find_ffmpeg_bin
from app.services.script_video.models import VideoPlan
from app.services.script_video.timeline import TimelineCompiler, VisualBeat

logger = get_logger(__name__)


class ScriptVideoAssemblyError(RuntimeError):
    pass


class ScriptVideoAssembler:
    width = 1080
    height = 1920
    fps = 30
    crossfade_seconds = TimelineCompiler.transition_seconds

    @staticmethod
    async def audio_duration(audio_path: Path) -> float:
        ffmpeg = Path(_find_ffmpeg_bin())
        ffprobe = shutil.which("ffprobe") or str(ffmpeg.with_name("ffprobe"))
        proc = await asyncio.create_subprocess_exec(
            ffprobe, "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(audio_path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        except asyncio.TimeoutError as exc:
            proc.kill()
            await proc.communicate()
            raise ScriptVideoAssemblyError("ffprobe timed out reading narration") from exc
        if proc.returncode:
            raise ScriptVideoAssemblyError(
                f"ffprobe could not read narration: {stderr.decode(errors='replace')[-300:]}"
            )
        try:
            duration = float(stdout.decode().strip())
        except (ValueError, UnicodeDecodeError) as exc:
            raise ScriptVideoAssemblyError("ffprobe returned an invalid narration duration") from exc
        if duration <= 0:
            raise ScriptVideoAssemblyError("Narration duration must be positive")
        return duration

    @staticmethod
    def _motion_filter(beat: VisualBeat, index: int, duration: float) -> str:
        frames = max(2, int(round(duration * ScriptVideoAssembler.fps)))
        progress = f"min(on/{frames},1)"
        zoom_in = f"min(1+0.06*{progress},1.06)"
        zoom_out = f"max(1.06-0.06*{progress},1)"
        if beat.motion == "zoom_out":
            zoom = zoom_out
        elif beat.motion in {"pan_left", "pan_right"}:
            zoom = "1.06"
        else:
            zoom = zoom_in
        if beat.motion == "pan_left":
            x = f"(iw-iw/zoom)*(0.5-0.3*{progress})"
            y = "ih/2-(ih/zoom/2)"
        elif beat.motion == "pan_right":
            x = f"(iw-iw/zoom)*(0.2+0.3*{progress})"
            y = "ih/2-(ih/zoom/2)"
        else:
            x = "iw/2-(iw/zoom/2)"
            y = "ih/2-(ih/zoom/2)"
        return (
            f"[{index}:v]scale={ScriptVideoAssembler.width}:{ScriptVideoAssembler.height}:"
            f"force_original_aspect_ratio=increase,crop={ScriptVideoAssembler.width}:{ScriptVideoAssembler.height},"
            f"zoompan=z='{zoom}':x='{x}':y='{y}':d=1:s={ScriptVideoAssembler.width}x"
            f"{ScriptVideoAssembler.height}:fps={ScriptVideoAssembler.fps},"
            f"trim=duration={duration:.4f},fps={ScriptVideoAssembler.fps},"
            f"settb=1/{ScriptVideoAssembler.fps},setpts=N,setsar=1,format=yuv420p[v{index}]"
        )

    def _filter_graph(self, beats: list[VisualBeat], durations: list[float]) -> str:
        graph = [self._motion_filter(beat, i, durations[i]) for i, beat in enumerate(beats)]
        groups: list[list[int]] = [[0]]
        for index in range(1, len(beats)):
            if beats[index].transition == "cut":
                groups.append([index])
            else:
                groups[-1].append(index)

        group_outputs: list[str] = []
        for group_no, group in enumerate(groups):
            current = f"v{group[0]}"
            elapsed = durations[group[0]]
            for beat_no, index in enumerate(group[1:], start=1):
                output = f"x{group_no}_{beat_no}"
                transition = {"slide_left": "slideleft", "slide_right": "slideright"}.get(
                    beats[index].transition, "fade"
                )
                offset = max(0.01, elapsed - self.crossfade_seconds)
                graph.append(
                    f"[{current}][v{index}]xfade=transition={transition}:duration={self.crossfade_seconds}:"
                    f"offset={offset:.4f}[{output}]"
                )
                elapsed += durations[index] - self.crossfade_seconds
                current = output
            group_outputs.append(current)

        if len(group_outputs) > 1:
            graph.append("".join(f"[{label}]" for label in group_outputs)
                         + f"concat=n={len(group_outputs)}:v=1:a=0,format=yuv420p[vout]")
        else:
            graph.append(f"[{group_outputs[0]}]format=yuv420p[vout]")
        return ";".join(graph)

    async def assemble(
        self,
        plan: VideoPlan,
        images: list[str],
        subtitle_path: Optional[str] = None,
        audio_path: Optional[Path] = None,
        output_path: Optional[Path] = None,
    ) -> Path:
        if not images:
            raise ScriptVideoAssemblyError("No images provided for assembly")
        image_paths = [Path(p).resolve() for p in images]
        if any(not p.is_file() for p in image_paths):
            raise ScriptVideoAssemblyError("An image file is missing")
        if audio_path is None or not Path(audio_path).is_file():
            raise ScriptVideoAssemblyError("Narration audio is missing")

        output_path = Path(output_path) if output_path else Path("/tmp") / f"script_{uuid.uuid4().hex}.mp4"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temp_dir = Path(tempfile.mkdtemp(prefix="recut_assembly_", dir=str(output_path.parent)))
        try:
            audio_duration = await self.audio_duration(Path(audio_path))
            beats = TimelineCompiler().compile(plan, len(image_paths), audio_duration)
            beat_durations = [beat.duration for beat in beats]
            filter_graph = self._filter_graph(beats, beat_durations)
            graph_path = temp_dir / "filter_complex.txt"
            subtitle_filter = ""
            if subtitle_path and Path(subtitle_path).is_file():
                escaped = str(Path(subtitle_path).resolve()).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
                subtitle_filter = (
                    f"subtitles='{escaped}':force_style='FontName=Arial,FontSize=56,"
                    "PrimaryColour=&H00FFFFFF,SecondaryColour=&H0000D7FF,"
                    "OutlineColour=&H000000,BorderStyle=1,Outline=4,MarginV=60'"
                )
                filter_graph += f";[vout]{subtitle_filter}[vfinal]"
            graph_path.write_text(filter_graph, encoding="utf-8")
            command = [_find_ffmpeg_bin(), "-y", "-v", "error"]
            for beat in beats:
                command.extend([
                    "-loop", "1", "-framerate", str(self.fps), "-t", f"{beat.duration:.4f}",
                    "-i", str(image_paths[beat.image_index]),
                ])
            command.extend(["-i", str(Path(audio_path).resolve())])
            video_label = "vfinal" if subtitle_filter else "vout"
            command.extend([
                "-filter_complex_script", str(graph_path), "-map", f"[{video_label}]",
                "-map", f"{len(beats)}:a:0", "-c:v", "libx264", "-preset", "fast",
                "-crf", "23", "-r", str(self.fps), "-pix_fmt", "yuv420p", "-c:a", "aac",
                "-b:a", "128k", "-shortest", "-movflags", "+faststart", str(output_path),
            ])
            proc = await asyncio.create_subprocess_exec(
                *command, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                raise ScriptVideoAssemblyError("FFmpeg timed out")
            if proc.returncode:
                logger.error("script_video_ffmpeg_failed", exit_code=proc.returncode,
                             stderr=(stderr or b"").decode(errors="replace")[-1000:])
                raise ScriptVideoAssemblyError("FFmpeg failed to assemble video")
            if not output_path.is_file() or output_path.stat().st_size == 0:
                raise ScriptVideoAssemblyError("FFmpeg produced an empty video")
            logger.info("script_video_assembly_complete", image_count=len(image_paths),
                        visual_beats=len(beats), audio_duration=audio_duration,
                        output_bytes=output_path.stat().st_size)
            return output_path
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
