"""Assemble hybrid stock video, AI stills, narration, and subtitles."""
from __future__ import annotations

import asyncio
import shutil
import tempfile
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Optional, Sequence

from PIL import Image, ImageChops

from app.core.logging import get_logger
from app.services.media.audio import AudioConfig, AudioProcessor
from app.services.media.ffmpeg import _find_ffmpeg_bin
from app.services.media.probe import ProbeError, get_probe_service
from app.services.script_video.models import VideoPlan
from app.services.script_video.timeline import TimelineCompiler, VisualBeat

logger = get_logger(__name__)


class ScriptVideoAssemblyError(RuntimeError):
    pass


class ScriptVideoAssembler:
    width = 1080
    height = 1920
    fps = 30
    intermediate_crf = 18
    final_crf = 23
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
        # Limit travel per second and maximum magnification. A short reaction
        # gets a hold, while a long important reveal has room for a slow push.
        amount = min(0.055, max(0.018, run_frames / ScriptVideoAssembler.fps * 0.004))
        if beat.importance >= 4:
            amount = min(0.06, amount * 1.1)
        if motion == "static":
            zoom = "1"
        elif motion == "zoom_out":
            zoom = f"max(1+{amount:.5f}-{amount:.5f}*({eased}),1)"
        elif motion in {"pan_left", "pan_right"}:
            zoom = f"{1 + amount:.5f}"
        else:
            zoom = f"min(1+{amount:.5f}*({eased}),1+{amount:.5f})"
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
    def _safe_framing(cls, beats: list[VisualBeat], assets: list[Path]) -> list[VisualBeat]:
        """Only reframe when exact flat borders prove no foreground is removed.

        This locates empty padding, not faces/objects. Natural photographs
        generally fail this conservative gate and keep their original framing.
        """
        scales: dict[Path, float] = {}
        result: list[VisualBeat] = []
        for beat in beats:
            asset = assets[beat.image_index]
            if result and assets[result[-1].image_index] == asset and beat.importance >= 4:
                if asset not in scales:
                    scales[asset] = 1.0
                    if not cls._is_video_asset(asset):
                        try:
                            with Image.open(asset) as source:
                                w, h = source.size
                                if (w >= 720 and h >= 1280 and w * h <= 8_000_000
                                        and abs(w / h - cls.width / cls.height) < .01):
                                    rgb = source.convert("RGB")
                                    box = ImageChops.difference(
                                        rgb, Image.new("RGB", rgb.size, rgb.getpixel((0, 0)))
                                    ).getbbox()
                                    if box:
                                        margin = min(box[0] / w, box[1] / h,
                                                     1 - box[2] / w, 1 - box[3] / h)
                                        if margin >= 0.09:
                                            scales[asset] = min(1.08, 1 / (1 - 2 * (margin - .06)))
                        except (OSError, ValueError, Image.DecompressionBombError):
                            pass
                # Do not pull out again on an arbitrary following beat.
                beat = replace(beat, framing_scale=max(result[-1].framing_scale, scales[asset]))
            elif result and assets[result[-1].image_index] == asset:
                beat = replace(beat, framing_scale=result[-1].framing_scale)
            result.append(beat)
        return result

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
                and beats[stop].framing_scale == beats[cursor].framing_scale
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
    def _merge_contiguous_beats(
        cls, beats: list[VisualBeat], assets: list[Path]
    ) -> list[VisualBeat]:
        """Render reused stills once; preserve the entry transition and story clock.

        Separate encodes/crossfades of the same photo add no distinct shot and
        can restart motion or blend an image against its own different scale.
        Graphic clips merge only within the original scene.
        """
        merged: list[VisualBeat] = []
        for beat in beats:
            asset = assets[beat.image_index]
            is_graphic = (
                asset.suffix.lower() == ".mp4"
                and asset.name.startswith("hyperframes-")
            )
            if (
                merged
                and assets[merged[-1].image_index] == asset
                and merged[-1].framing_scale == beat.framing_scale
                and (not cls._is_video_asset(asset)
                     or (is_graphic and merged[-1].scene_id == beat.scene_id))
            ):
                previous = merged[-1]
                merged[-1] = VisualBeat(
                    scene_id=previous.scene_id,
                    image_index=previous.image_index,
                    duration=previous.duration + beat.duration - (
                        cls.crossfade_seconds if beat.transition != "cut" else 0.0
                    ),
                    motion=(beat.motion if beat.importance > previous.importance else previous.motion),
                    transition=previous.transition,
                    importance=max(previous.importance, beat.importance),
                    framing_scale=previous.framing_scale,
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
            f"tpad=stop_mode=clone:stop_duration={duration:.4f},"
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
        for attempt in range(2):
            proc = await asyncio.create_subprocess_exec(
                *command, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await self._communicate(
                proc, timeout=90, timeout_message="FFmpeg timed out rendering a visual beat",
            )
            if proc.returncode or not output_path.is_file() or output_path.stat().st_size == 0:
                logger.error(
                    "script_video_beat_render_failed", exit_code=proc.returncode,
                    stderr=stderr.decode(errors="replace")[-500:],
                )
                raise ScriptVideoAssemblyError("FFmpeg failed to render a visual beat")
            try:
                await get_probe_service().probe(output_path)
                return
            except ProbeError as exc:
                # A nonempty file and exit 0 were observed with an unfinished
                # MP4. Retry only that local encode, never provider generation.
                output_path.unlink(missing_ok=True)
                if attempt:
                    raise ScriptVideoAssemblyError("Rendered beat failed validation") from exc
                logger.warning("script_video_beat_retry", reason="invalid_mp4")

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
        # A framing change is allowed only after the flat-border safety gate.
        framing = (
            f"crop=trunc(iw/{beat.framing_scale:.5f}/2)*2:"
            f"trunc(ih/{beat.framing_scale:.5f}/2)*2,"
            if beat.framing_scale > 1 else ""
        )
        graph_path.write_text(
            self._motion_filter(
                beat, 0, duration,
                start_frame=start_frame,
                total_frames=total_frames,
                continuous_motion=continuous_motion,
            ).replace("[0:v]scale=", f"[0:v]{framing}scale=", 1),
            encoding="utf-8",
        )
        command = [
            _find_ffmpeg_bin(), "-y", "-v", "error", "-loop", "1", "-framerate",
            str(self.fps), "-t", f"{duration:.4f}", "-i", str(image_path),
            "-filter_complex_script", str(graph_path), "-map", "[v0]", "-an",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf",
            str(self.intermediate_crf), "-r",
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
        source_duration: float | None = None,
    ) -> None:
        graph_path = output_path.with_suffix(".filter.txt")
        graph_path.write_text(self._video_filter(duration), encoding="utf-8")
        if source_duration is None:
            source_duration = (await get_probe_service().probe(video_path)).duration_seconds
        seek_seconds = (
            0.0 if video_path.name.startswith("hyperframes-")
            else min((beat_index % 3) * 0.35, max(0.0, source_duration - 0.1))
        )
        command = [
            _find_ffmpeg_bin(), "-y", "-v", "error", "-ss", f"{seek_seconds:.2f}",
            "-i", str(video_path),
            "-filter_complex_script", str(graph_path), "-map", "[v0]", "-an",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf",
            str(self.intermediate_crf), "-r",
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
        source_duration: float | None = None,
    ) -> None:
        if self._is_video_asset(asset_path):
            await self._render_video_beat(
                asset_path, duration, output_path, beat_index,
                source_duration=source_duration,
            )
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
        phrases: Sequence[object] = (),
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
        temp_dir = Path(tempfile.mkdtemp(prefix="recut_assembly_"))
        try:
            audio_duration = await self.audio_duration(Path(audio_path))
            beats = TimelineCompiler().compile(
                plan, len(asset_paths), audio_duration, phrases=phrases, assets=asset_paths,
            )
            beats = self._safe_framing(beats, asset_paths)
            beats = self._merge_contiguous_beats(beats, asset_paths)
            beat_durations = [beat.duration for beat in beats]
            # Image indices may differ while resolving to the same shared file.
            # A single camera path across that run removes zoom jumps at cuts.
            still_windows = self._still_motion_windows(beats, asset_paths, beat_durations)
            video_durations = {}
            for asset_path in dict.fromkeys(asset_paths):
                if self._is_video_asset(asset_path):
                    video_durations[asset_path] = (
                        await get_probe_service().probe(asset_path)
                    ).duration_seconds
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
                    source_duration=video_durations.get(asset_paths[beat.image_index]),
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
            audio_args, _ = AudioProcessor().audio_args(AudioConfig(preset="original"))
            video_label = "vfinal" if subtitle_filter else "vout"
            command.extend([
                "-filter_complex_script", str(graph_path), "-map", f"[{video_label}]",
                "-map", f"{len(beats)}:a:0", *audio_args,
                "-c:v", "libx264", "-preset", "fast",
                "-crf", str(self.final_crf), "-r", str(self.fps), "-pix_fmt", "yuv420p",
                "-c:a", "aac",
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
            try:
                result = await get_probe_service().probe(output_path)
            except ProbeError as exc:
                raise ScriptVideoAssemblyError("Rendered video failed ffprobe validation") from exc
            if not (
                (result.width, result.height) == (self.width, self.height)
                and result.video_codec == "h264"
                and result.audio_codec == "aac"
                and result.has_audio
                and result.rotation == 0
                and abs(result.sample_aspect_ratio - 1.0) <= 0.01
                and abs(result.fps - self.fps) <= 0.1
                and abs(result.duration_seconds - audio_duration) <= 0.5
            ):
                raise ScriptVideoAssemblyError("Rendered video has unexpected media properties")
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
