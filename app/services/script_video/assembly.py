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
    async def _communicate(
        proc: asyncio.subprocess.Process,
        *,
        timeout: float,
        timeout_message: str,
    ) -> tuple[bytes, bytes]:
        """Wait for a child process and never leave it alive after cancellation."""
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return stdout or b"", stderr or b""
        except asyncio.CancelledError:
            if proc.returncode is None:
                proc.kill()
            await proc.communicate()
            raise
        except asyncio.TimeoutError as exc:
            if proc.returncode is None:
                proc.kill()
            await proc.communicate()
            raise ScriptVideoAssemblyError(timeout_message) from exc

    @staticmethod
    async def audio_duration(audio_path: Path) -> float:
        ffmpeg = Path(_find_ffmpeg_bin())
        ffprobe = shutil.which("ffprobe") or str(ffmpeg.with_name("ffprobe"))
        proc = await asyncio.create_subprocess_exec(
            ffprobe, "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(audio_path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await ScriptVideoAssembler._communicate(
            proc,
            timeout=30,
            timeout_message="ffprobe timed out reading narration",
        )
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
    def _motion_filter(
        beat: VisualBeat,
        index: int,
        duration: float,
        *,
        start_frame: int = 0,
        total_frames: int | None = None,
        continuous_motion: str | None = None,
    ) -> str:
        """Animate a whole run of identical stills without restarting the zoom.

        A visual plan can reuse an AI frame for two or three neighbouring
        scenes. Those scenes are rendered separately, but the animation must
        retain its exact camera position at the cut. The frame offset is based
        on encoded frame counts rather than floating point seconds.
        """
        frames = max(2, int(round(duration * ScriptVideoAssembler.fps)))
        run_frames = max(frames, total_frames or frames)
        # Use one progress function for an entire repeated-image run.
        progress = f"min(max((on+{max(0, start_frame)})/{run_frames},0),1)"
        # Easing is continuous at every beat boundary and slows at the ends.
        eased = f"({progress})*({progress})*(3-2*({progress}))"
        motion = continuous_motion or beat.motion
        if motion == "static":
            zoom = "1"
        elif motion == "zoom_out":
            zoom = f"max(1.045-0.045*({eased}),1)"
        elif motion in {"pan_left", "pan_right"}:
            zoom = "1.045"
        else:
            zoom = f"min(1+0.045*({eased}),1.045)"
        if motion == "pan_left":
            x = f"(iw-iw/zoom)*(0.72-0.44*({eased}))"
        elif motion == "pan_right":
            x = f"(iw-iw/zoom)*(0.28+0.44*({eased}))"
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
    def _still_motion_windows(
        cls,
        beats: list[VisualBeat],
        assets: list[Path],
        durations: list[float],
    ) -> list[tuple[int, int, str]]:
        """Give consecutive beats using the same still a shared camera path."""
        windows: list[tuple[int, int, str]] = [
            (0, max(2, round(duration * cls.fps)), beat.motion)
            for beat, duration in zip(beats, durations)
        ]
        cursor = 0
        while cursor < len(beats):
            asset = assets[beats[cursor].image_index]
            if cls._is_video_asset(asset):
                cursor += 1
                continue
            stop = cursor + 1
            while (
                stop < len(beats)
                and assets[beats[stop].image_index] == asset
            ):
                stop += 1
            lengths = [max(2, round(durations[i] * cls.fps)) for i in range(cursor, stop)]
            full_length = sum(lengths)
            offset = 0
            motion = beats[cursor].motion
            for idx, length in zip(range(cursor, stop), lengths):
                windows[idx] = (offset, full_length, motion)
                offset += length
            cursor = stop
        return windows

    @classmethod
    def _merge_hyperframes_beats(
        cls, beats: list[VisualBeat], assets: list[Path]
    ) -> list[VisualBeat]:
        """Keep a motion-graphic scene continuous, not split into restart clips.

        The general short-beat editor deliberately divides longer visuals into
        multiple beats. That is fine for photos, but rendering the same 3-5s
        HyperFrames clip repeatedly resets its CSS motion and causes a stutter.
        Merge only adjacent beats of the *same scene* using the same graphic.
        All other assets and editorial beats keep their original treatment.
        """
        merged: list[VisualBeat] = []
        for beat in beats:
            asset = assets[beat.image_index]
            is_graphic = (
                asset.suffix.lower() == ".mp4"
                and asset.name.startswith("hyperframes-")
            )
            if (
                is_graphic
                and merged
                and merged[-1].scene_id == beat.scene_id
                and merged[-1].image_index == beat.image_index
            ):
                previous = merged[-1]
                merged[-1] = VisualBeat(
                    scene_id=previous.scene_id,
                    image_index=previous.image_index,
                    duration=previous.duration + beat.duration,
                    motion=previous.motion,
                    transition=previous.transition,
                )
            else:
                merged.append(beat)
        return merged

    @classmethod
    def _video_filter(cls, duration: float) -> str:
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
            f"[{i}:v]setpts=PTS-STARTPTS,fps={self.fps},settb=1/{self.fps},"
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
        _, stderr = await self._communicate(
            proc,
            timeout=90,
            timeout_message="FFmpeg timed out rendering a visual beat",
        )
        if proc.returncode or not output_path.is_file() or output_path.stat().st_size == 0:
            logger.error(
                "script_video_beat_render_failed",
                exit_code=proc.returncode,
                stderr=stderr.decode(errors="replace")[-500:],
            )
            raise ScriptVideoAssemblyError("FFmpeg failed to render a visual beat")

    async def _render_image_beat(
        self,
        beat: VisualBeat,
        image_path: Path,
        duration: float,
        output_path: Path,
        *,
        start_frame: int = 0,
        total_frames: int | None = None,
        continuous_motion: str | None = None,
    ) -> None:
        graph_path = output_path.with_suffix(".filter.txt")
        graph_path.write_text(
            self._motion_filter(
                beat, 0, duration,
                start_frame=start_frame,
                total_frames=total_frames,
                continuous_motion=continuous_motion,
            ),
            encoding="utf-8",
        )
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
        seek_seconds = (
            0.0 if video_path.name.startswith("hyperframes-")
            else (beat_index % 3) * 0.35
        )
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
        *,
        still_window: tuple[int, int, str] | None = None,
    ) -> None:
        if self._is_video_asset(asset_path):
            await self._render_video_beat(asset_path, duration, output_path, beat_index)
        else:
            frame, frames, motion = still_window or (
                0, max(2, round(duration * self.fps)), beat.motion
            )
            await self._render_image_beat(
                beat, asset_path, duration, output_path,
                start_frame=frame,
                total_frames=frames,
                continuous_motion=motion,
            )

    async def assemble(
        self,
        plan: VideoPlan,
        images: list[str],
        subtitle_path: Optional[str] = None,
        audio_path: Optional[Path] = None,
        output_path: Optional[Path] = None,
    ) -> Path:
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
            beats = self._merge_hyperframes_beats(beats, asset_paths)
            beat_durations = [beat.duration for beat in beats]
            # Image indices may differ while resolving to the same shared file.
            # A single camera path across that run removes zoom jumps at cuts.
            still_windows = self._still_motion_windows(beats, asset_paths, beat_durations)
            clip_paths: list[Path] = []
            for index, beat in enumerate(beats):
                clip_path = temp_dir / f"beat-{index:03d}.mp4"
                await self._render_beat(
                    beat,
                    asset_paths[beat.image_index],
                    beat_durations[index],
                    clip_path,
                    index,
                    still_window=still_windows[index],
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
                subtitle_filter = f"subtitles='{escaped}'"
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
            _, stderr = await self._communicate(
                proc,
                timeout=300,
                timeout_message="FFmpeg timed out",
            )
            if proc.returncode:
                stderr_text = stderr.decode(errors="replace")
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
                    _, stderr = await self._communicate(
                        proc,
                        timeout=300,
                        timeout_message="FFmpeg timed out on cut-transition fallback",
                    )
                    if proc.returncode == 0:
                        logger.info("script_video_cut_transition_fallback_completed")
                if proc.returncode:
                    logger.error(
                        "script_video_ffmpeg_failed",
                        exit_code=proc.returncode,
                        stderr=stderr.decode(errors="replace")[-1000:],
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
