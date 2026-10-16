"""Assemble hybrid stock video, AI stills, narration, and subtitles."""
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
    video_suffixes = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}

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

    @classmethod
    def _is_video_asset(cls, path: Path) -> bool:
        return path.suffix.lower() in cls.video_suffixes

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

    @classmethod
    def _video_filter(cls, duration: float) -> str:
        # Keep the full source visible even when a stock clip is landscape. A
        # blurred fill is less destructive than an aggressive center crop.
        return (
            f"[0:v]split=2[bg][fg];"
            f"[bg]scale={cls.width}:{cls.height}:force_original_aspect_ratio=increase,"
            f"crop={cls.width}:{cls.height},gblur=sigma=28[bg2];"
            f"[fg]scale={cls.width}:{cls.height}:force_original_aspect_ratio=decrease[fg2];"
            f"[bg2][fg2]overlay=(W-w)/2:(H-h)/2,"
            f"trim=duration={duration:.4f},fps={cls.fps},settb=1/{cls.fps},"
            f"setpts=N,setsar=1,format=yuv420p[v0]"
        )

    def _filter_graph(self, beats: list[VisualBeat], durations: list[float]) -> str:
        graph = [
            f"[{i}:v]fps={self.fps},settb=AVTB,setpts=PTS-STARTPTS,"
            f"setsar=1,format=yuv420p[v{i}]"
            for i in range(len(beats))
        ]
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
            graph.append(
                "".join(f"[{label}]" for label in group_outputs)
                + f"concat=n={len(group_outputs)}:v=1:a=0,format=yuv420p[vout]"
            )
        else:
            graph.append(f"[{group_outputs[0]}]format=yuv420p[vout]")
        return ";".join(graph)

    async def _run_beat_command(self, command: list[str], output_path: Path) -> None:
        proc = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=90)
        except asyncio.TimeoutError as exc:
            proc.kill()
            await proc.communicate()
            raise ScriptVideoAssemblyError("FFmpeg timed out rendering a visual beat") from exc
        if proc.returncode or not output_path.is_file() or output_path.stat().st_size == 0:
            logger.error(
                "script_video_beat_render_failed",
                exit_code=proc.returncode,
                stderr=(stderr or b"").decode(errors="replace")[-500:],
            )
            raise ScriptVideoAssemblyError("FFmpeg failed to render a visual beat")

    async def _render_image_beat(
        self,
        beat: VisualBeat,
        image_path: Path,
        duration: float,
        output_path: Path,
    ) -> None:
        graph_path = output_path.with_suffix(".filter.txt")
        graph_path.write_text(self._motion_filter(beat, 0, duration), encoding="utf-8")
        command = [
            _find_ffmpeg_bin(), "-y", "-v", "error", "-loop", "1", "-framerate",
            str(self.fps), "-t", f"{duration:.4f}", "-i", str(image_path),
            "-filter_complex_script", str(graph_path), "-map", "[v0]", "-an",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23", "-r",
            str(self.fps), "-pix_fmt", "yuv420p", "-t", f"{duration:.4f}",
            "-movflags", "+faststart", str(output_path),
        ]
        try:
            await self._run_beat_command(command, output_path)
        finally:
            graph_path.unlink(missing_ok=True)

    async def _render_video_beat(
        self,
        video_path: Path,
        duration: float,
        output_path: Path,
        beat_index: int,
    ) -> None:
        graph_path = output_path.with_suffix(".filter.txt")
        graph_path.write_text(self._video_filter(duration), encoding="utf-8")
        # Offset repeated uses slightly so a duplicated source does not restart at
        # the exact same frame. stream_loop keeps short stock clips long enough.
        seek_seconds = (beat_index % 3) * 0.35
        command = [
            _find_ffmpeg_bin(), "-y", "-v", "error", "-stream_loop", "-1",
            "-ss", f"{seek_seconds:.2f}", "-i", str(video_path),
            "-filter_complex_script", str(graph_path), "-map", "[v0]", "-an",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23", "-r",
            str(self.fps), "-pix_fmt", "yuv420p", "-t", f"{duration:.4f}",
            "-movflags", "+faststart", str(output_path),
        ]
        try:
            await self._run_beat_command(command, output_path)
        finally:
            graph_path.unlink(missing_ok=True)

    async def _render_beat(
        self,
        beat: VisualBeat,
        asset_path: Path,
        duration: float,
        output_path: Path,
        beat_index: int,
    ) -> None:
        if self._is_video_asset(asset_path):
            await self._render_video_beat(asset_path, duration, output_path, beat_index)
        else:
            await self._render_image_beat(beat, asset_path, duration, output_path)

    async def assemble(
        self,
        plan: VideoPlan,
        images: list[str],
        subtitle_path: Optional[str] = None,
        audio_path: Optional[Path] = None,
        output_path: Optional[Path] = None,
    ) -> Path:
        # Keep the public parameter name `images` for backwards compatibility;
        # Quality V3 accepts either image paths or video paths in this list.
        if not images:
            raise ScriptVideoAssemblyError("No visual assets provided for assembly")
        asset_paths = [Path(p).resolve() for p in images]
        if any(not p.is_file() for p in asset_paths):
            raise ScriptVideoAssemblyError("A visual asset file is missing")
        if audio_path is None or not Path(audio_path).is_file():
            raise ScriptVideoAssemblyError("Narration audio is missing")

        output_path = Path(output_path) if output_path else Path("/tmp") / f"script_{uuid.uuid4().hex}.mp4"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temp_dir = Path(tempfile.mkdtemp(prefix="recut_assembly_", dir=str(output_path.parent)))
        try:
            audio_duration = await self.audio_duration(Path(audio_path))
            beats = TimelineCompiler().compile(plan, len(asset_paths), audio_duration)
            beat_durations = [beat.duration for beat in beats]
            clip_paths: list[Path] = []
            for index, beat in enumerate(beats):
                clip_path = temp_dir / f"beat-{index:03d}.mp4"
                await self._render_beat(
                    beat,
                    asset_paths[beat.image_index],
                    beat_durations[index],
                    clip_path,
                    index,
                )
                clip_paths.append(clip_path)
            filter_graph = self._filter_graph(beats, beat_durations)
            graph_path = temp_dir / "filter_complex.txt"
            subtitle_filter = ""
            if subtitle_path and Path(subtitle_path).is_file():
                escaped = (
                    str(Path(subtitle_path).resolve())
                    .replace("\\", "\\\\")
                    .replace(":", "\\:")
                    .replace("'", "\\'")
                )
                subtitle_filter = (
                    f"subtitles='{escaped}':force_style='FontName=Arial,FontSize=56,"
                    "PrimaryColour=&H00FFFFFF,SecondaryColour=&H0000D7FF,"
                    "OutlineColour=&H000000,BorderStyle=1,Outline=4,MarginV=60'"
                )
                filter_graph += f";[vout]{subtitle_filter}[vfinal]"
            graph_path.write_text(filter_graph, encoding="utf-8")
            command = [_find_ffmpeg_bin(), "-y", "-v", "error"]
            for clip_path in clip_paths:
                command.extend(["-i", str(clip_path)])
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
                stderr_text = (stderr or b"").decode(errors="replace")
                if "xfade" in stderr_text and "frame rate" in stderr_text.lower():
                    logger.warning("script_video_transition_fallback", reason="invalid_xfade_framerate")
                    cut_beats = [
                        VisualBeat(
                            scene_id=beat.scene_id,
                            image_index=beat.image_index,
                            duration=beat.duration,
                            motion=beat.motion,
                            transition="cut",
                        )
                        for beat in beats
                    ]
                    fallback_graph = self._filter_graph(cut_beats, beat_durations)
                    if subtitle_filter:
                        fallback_graph += f";[vout]{subtitle_filter}[vfinal]"
                    graph_path.write_text(fallback_graph, encoding="utf-8")
                    proc = await asyncio.create_subprocess_exec(
                        *command,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    try:
                        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
                    except asyncio.TimeoutError as exc:
                        proc.kill()
                        await proc.communicate()
                        raise ScriptVideoAssemblyError("FFmpeg timed out on cut-transition fallback") from exc
                    if proc.returncode == 0:
                        logger.info("script_video_cut_transition_fallback_completed")
                if proc.returncode:
                    logger.error(
                        "script_video_ffmpeg_failed",
                        exit_code=proc.returncode,
                        stderr=(stderr or b"").decode(errors="replace")[-1000:],
                    )
                    raise ScriptVideoAssemblyError("FFmpeg failed to assemble video")
            if not output_path.is_file() or output_path.stat().st_size == 0:
                raise ScriptVideoAssemblyError("FFmpeg produced an empty video")
            logger.info(
                "script_video_assembly_complete",
                asset_count=len(asset_paths),
                stock_video_count=sum(1 for path in asset_paths if self._is_video_asset(path)),
                visual_beats=len(beats),
                audio_duration=audio_duration,
                output_bytes=output_path.stat().st_size,
            )
            return output_path
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
