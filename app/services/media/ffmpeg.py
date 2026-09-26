"""Media service for FFmpeg operations (future video support)."""

import asyncio
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from app.core.logging import get_logger


logger = get_logger(__name__)

_FONT_CANDIDATES = [
    # Debian (Railway Docker: fonts-dejavu-core)
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    # Windows dev machine
    "C:/Windows/Fonts/arial.ttf",
    "C:/Windows/Fonts/seguisb.ttf",
]


def _find_font() -> str | None:
    """First existing TTF for drawtext (None → ffmpeg default)."""
    for p in _FONT_CANDIDATES:
        if Path(p).exists():
            return p
    return None


def _asset_pixel_size(path: Path, max_w: int, max_h: int) -> tuple[int, int]:
    """Pixel size of an image/video asset, CONTAIN-fit into (max_w, max_h).

    Even dims. Falls back to the box size itself when probing fails.
    Synchronous ffprobe (short) — safe inside a running event loop.
    """
    try:
        ffbin = _find_ffmpeg_bin()
        ffprobe = str(Path(ffbin).with_name("ffprobe.exe"))
        if not Path(ffprobe).exists():
            ffprobe = str(Path(ffbin).with_name("ffprobe"))
        r = subprocess.run(
            [ffprobe, "-v", "error", "-print_format", "json",
             "-show_streams", str(path)],
            capture_output=True, text=True, timeout=30, check=True,
        )
        import json as _json
        data = _json.loads(r.stdout)
        vs = next(s for s in data.get("streams", [])
                  if s.get("codec_type") == "video")
        w, h = int(vs.get("width") or 0), int(vs.get("height") or 0)
        if w > 0 and h > 0:
            scale = min(1.0, max_w / w, max_h / h)
            return max(2, int(w * scale) // 2 * 2), max(2, int(h * scale) // 2 * 2)
    except Exception:
        pass
    return max(2, max_w), max(2, max_h)


def _find_ffmpeg_bin() -> str:
    return shutil.which("ffmpeg") or "ffmpeg"


def _drawtext(text: str, size_px: int, x: str, y: str,
              alpha: str = "1.0", borderw: int = 2) -> str:
    """drawtext filter with escaping and explicit fontfile when found."""
    safe = (
        text.replace("\\", "\\\\").replace(":", "\\:")
        .replace("'", "\\'").replace("%", "\\%")
    )
    parts = [
        f"drawtext=text='{safe}'",
        f"fontsize={size_px}",
        f"fontcolor=white@{alpha}",
        f"borderw={borderw}",
        "bordercolor=black",
        f"x={x}",
        f"y={y}",
    ]
    font = _find_font()
    if font:
        # ':' inside the filtergraph must be escaped even inside quotes.
        parts.insert(1, f"fontfile='{font.replace(':', chr(92) + ':')}'")
    return ":".join(parts)


class MediaService:
    """FFmpeg wrapper for media processing."""

    def __init__(self) -> None:
        self._ffmpeg_path = self._find_ffmpeg()

    def _find_ffmpeg(self) -> str:
        """Find FFmpeg executable."""
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            logger.warning("ffmpeg_not_found", path="ffmpeg not in PATH")
            return "ffmpeg"  # Will fail at runtime if not installed
        logger.info("ffmpeg_found", path=ffmpeg)
        return ffmpeg

    async def normalize_square_pixels(
        self, input_path: Path, output_path: Path, timeout: int = 120,
    ) -> Path:
        """Make SAR 1:1 while PRESERVING the display aspect ratio.

        Anamorphic source (SAR != 1): coded 720x576 SAR 16:15 displays as
        768x576. We scale the coded pixels up to the DISPLAY size
        (w='trunc(iw*sar/2)*2':h='ih') and set SAR 1:1 — the picture is
        then square-pixel with IDENTICAL visual proportions, never
        stretched. Square-pixel sources are stream-copied untouched.

        Verified with ffprobe afterwards: output SAR must be 1:1.
        """
        if not input_path.exists():
            raise FileNotFoundError(f"Input not found: {input_path}")
        output_path.parent.mkdir(parents=True, exist_ok=True)

        from app.services.media.probe import get_probe_service
        meta = await get_probe_service().probe(input_path)
        is_square = abs(meta.sample_aspect_ratio - 1.0) <= 0.01

        cmd = [self._ffmpeg_path, "-y", "-v", "error", "-i", str(input_path)]
        if is_square:
            # Already square pixels — remux only (no visual change).
            cmd += ["-c", "copy"]
        else:
            # Expand coded pixels to display size, then force square SAR.
            # scale expression `sar` = input sample aspect ratio.
            cmd += [
                "-vf", "scale=w='trunc(iw*sar/2)*2':h='ih',setsar=1",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                "-c:a", "copy",
            ]
        cmd += [
            "-map_metadata", "-1", "-map_chapters", "-1",
            "-movflags", "+faststart",
            str(output_path),
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        if proc.returncode != 0:
            raise RuntimeError(
                "normalize_square_pixels failed: "
                + stderr.decode(errors="ignore")[:300]
            )

        # Verify: output SAR must be 1:1 (PART 4 acceptance).
        out_meta = await get_probe_service().probe(output_path)
        if abs(out_meta.sample_aspect_ratio - 1.0) > 0.01:
            raise RuntimeError(
                f"normalize_square_pixels: output SAR still "
                f"{out_meta.sample_aspect_ratio}, expected 1:1"
            )
        logger.info(
            "normalize_square_pixels_done",
            input_sar=meta.sample_aspect_ratio,
            output=f"{out_meta.width}x{out_meta.height}",
            output_sar=out_meta.sample_aspect_ratio,
            output_dar=out_meta.display_aspect_ratio,
        )
        return output_path

    # Backwards-compatible alias (old name kept until all callers migrate).
    async def _run_sar_fix(self, input_path: Path, output_path: Path, timeout: int = 120) -> None:
        """Deprecated alias for normalize_square_pixels()."""
        await self.normalize_square_pixels(input_path, output_path, timeout)

    async def extract_audio(
        self,
        input_path: Path,
        output_path: Path,
        *,
        sample_rate: int = 16_000,
        channels: int = 1,
        sample_format: str = "s16",
        timeout_seconds: float = 300.0,
    ) -> Path:
        """Extract a mono PCM track from `input_path` to `output_path`.

        Default format matches faster-whisper's expectations:
        16 kHz, 1 channel, signed 16-bit PCM WAV. Other downstream
        consumers (subtitle stage, normalization) can re-encode.

        Uses `asyncio.create_subprocess_exec` (shell=False) so the
        user-controlled `input_path` is passed as a literal argv token.
        """
        if not input_path.exists():
            raise FileNotFoundError(f"Input not found: {input_path}")
        output_path.parent.mkdir(parents=True, exist_ok=True)

        cmd = [
            self._ffmpeg_path,
            "-y",                     # overwrite output
            "-v", "error",            # quiet, only errors
            "-i", str(input_path),
            "-vn",                    # strip video stream
            "-ac", str(channels),
            "-ar", str(sample_rate),
            "-acodec", "pcm_s16le" if sample_format == "s16" else sample_format,
            str(output_path),
        ]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_seconds,
            )
        except asyncio.TimeoutError as e:
            raise RuntimeError(f"FFmpeg extract_audio timed out after {timeout_seconds}s") from e

        if proc.returncode != 0:
            err = stderr.decode(errors="ignore")[:500]
            logger.error(
                "ffmpeg_extract_audio_failed",
                input=str(input_path),
                error=err,
            )
            raise RuntimeError(f"FFmpeg extract_audio failed: {err}")

        if not output_path.exists() or output_path.stat().st_size == 0:
            raise RuntimeError(f"FFmpeg produced empty output at {output_path}")

        logger.info(
            "ffmpeg_extract_audio_done",
            input=str(input_path),
            output=str(output_path),
            bytes=output_path.stat().st_size,
        )
        return output_path

    async def cut_clip(
        self,
        input_path: Path,
        output_path: Path,
        start_seconds: float,
        end_seconds: float,
        *,
        timeout_seconds: float = 180.0,
    ) -> Path:
        """Cut a [start, end] segment out of `input_path` to `output_path`.

        Uses `-c copy` so the bitstream is re-muxed without re-encoding —
        fast (sub-second) and lossless. Frame-accurate cuts aren't guaranteed
        with copy mode (keyframes only); for cleaner cuts use the re-encode
        path in Stage G.

        Validation lives in the caller (ClipCutter); here we just run ffmpeg.
        """
        if not input_path.exists():
            raise FileNotFoundError(f"Input not found: {input_path}")
        if end_seconds <= start_seconds:
            raise ValueError(f"end ({end_seconds}) must be > start ({start_seconds})")

        output_path.parent.mkdir(parents=True, exist_ok=True)

        cmd = [
            self._ffmpeg_path,
            "-y",
            "-v", "error",
            "-ss", f"{start_seconds:.3f}",
            "-to", f"{end_seconds:.3f}",
            "-i", str(input_path),
            "-c", "copy",
            "-avoid_negative_ts", "make_zero",
            "-movflags", "+faststart",     # web-friendly MP4 header
            str(output_path),
        ]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_seconds,
            )
        except asyncio.TimeoutError as e:
            raise RuntimeError(f"FFmpeg cut timed out after {timeout_seconds}s") from e

        if proc.returncode != 0:
            err = stderr.decode(errors="ignore")[:500]
            logger.error(
                "ffmpeg_cut_failed",
                input=str(input_path),
                start=start_seconds, end=end_seconds,
                error=err,
            )
            raise RuntimeError(f"FFmpeg cut failed: {err}")

        if not output_path.exists() or output_path.stat().st_size == 0:
            raise RuntimeError(f"FFmpeg cut produced empty output at {output_path}")

        logger.info(
            "ffmpeg_cut_done",
            input=str(input_path),
            output=str(output_path),
            start=start_seconds, end=end_seconds,
            bytes=output_path.stat().st_size,
        )
        return output_path

    async def make_vertical(
        self,
        input_path: Path,
        output_path: Path,
        *,
        target_width: int = 1080,
        target_height: int = 1920,
        target_fps: int = 30,
        video_bitrate: str = "4M",
        audio_bitrate: str = "128k",
        blur_strength: int = 30,
        background_id: str = "blur",
        title_text: str = "",
        brand_corner: bool = False,
        decoration_id: str = "",
        audio_preset: str = "original",
        speed: float = 1.0,  # TZ Phase 16: setpts/atempo, pitch preserved
        color_preset: str = "original",  # TZ Phase 15: eq на контент
        layout_id: str = "pip",  # TZ Phase 17: full / pip / framed
        timeout_seconds: float = 600.0,
    ) -> Path:
        """Convert source video to 9:16 vertical with a styled background.

        background_id: 'blur' (default) — blurred copy of the source;
        'dark'/'light'/'accent' — solid color canvas (templates registry).
        title_text: burned at top safe area via drawtext (empty = off).
        brand_corner: small 'ReCut' tag in the top-right corner.

        Strategy (single ffmpeg filter_complex pass):
        - bg: blurred source copy OR solid color (templates registry)
        - fg: scale CONTAIN target (decrease, preserve aspect), setsar=1
        - overlay fg on bg (centred); optional title + brand corner text

        Audio is passed through; output H.264 + AAC, yuv420p, +faststart.
        """
        if not input_path.exists():
            raise FileNotFoundError(f"Input not found: {input_path}")
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # STEP 1 / #20: detect embedded letterbox/pillarbox black bars and
        # pre-crop real content BEFORE layout. Stable across >=70% of
        # sampled frames — a single random frame never decides.
        source = input_path
        # BLOCKER #4: SourceNormalizer already handles bars/rotation.
        # make_vertical does NOT crop again (single owner).

        # PART 2/4 — TemplateCompositor is the ONLY layout engine.
        # Python computes the foreground box from the DISPLAY aspect ratio
        # (probe → geometry.fit_inside); FFmpeg just executes
        # scale=FG_W:FG_H + overlay=x:y. No force_original_aspect_ratio,
        # no reset_sar on the foreground — Python decides the pixels.
        from app.services.media.geometry import get_display_geometry
        from app.services.overlays.compositor import TemplateSpec
        probe_svc = None
        try:
            from app.services.media.probe import get_probe_service
            probe_svc = get_probe_service()
            src_meta = await probe_svc.probe(source)
            src_geo = get_display_geometry(src_meta)
            fg_ratio = (src_geo.effective_width_after_rotation /
                        max(src_geo.effective_height_after_rotation, 1))
        except Exception as e:
            logger.warning("make_vertical_probe_failed_default_contain", error=str(e)[:200])
            # Square-pixel fallback: coded dims define the ratio.
            src_geo = None
            fg_ratio = 16 / 9  # resolved below via ffprobe fallback

        if src_geo is None:
            # Last resort: square-pixel coded dims define the ratio
            # (better a slightly wrong contain than any stretch).
            ffprobe = str(Path(self._ffmpeg_path).with_name("ffprobe.exe"))
            if not Path(ffprobe).exists():
                ffprobe = "ffprobe"
            r = await asyncio.create_subprocess_exec(
                ffprobe, "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=width,height", "-of", "csv=p=0",
                str(source), stdout=asyncio.subprocess.PIPE,
            )
            out, _ = await r.communicate()
            try:
                cw, ch = (int(x) for x in out.decode().strip().split(",")[:2])
                fg_ratio = cw / max(ch, 1)
            except Exception:
                fg_ratio = target_width / target_height
        fg = TemplateSpec.for_layout(layout_id, target_width, target_height).fit_video(fg_ratio)

        logger.info(
            "make_vertical_layout",
            source_coded=f"{src_geo.coded_width}x{src_geo.coded_height}" if src_geo else "?",
            source_sar=src_geo.sar if src_geo else None,
            source_dar=src_geo.dar if src_geo else round(fg_ratio, 3),
            source_rotation=src_geo.rotation if src_geo else 0,
            canvas=f"{target_width}x{target_height}",
            fg_box=f"{fg.width}x{fg.height}@{fg.x},{fg.y}",
        )

        from app.services.overlays.templates import BACKGROUNDS
        preset = BACKGROUNDS.get(background_id, BACKGROUNDS["blur"])
        # Foreground: EXPLICIT pixel size computed in Python (PART 4) —
        # FFmpeg performs, never re-decides aspect. bg keeps reset_sar=1
        # (cover-crop of a blurred canvas is aspect-agnostic).
        fg_scale = f"scale={fg.width}:{fg.height},setsar=1"
        # TZ Phase 15: color preset applies to the FOREGROUND content only.
        from app.services.overlays.presets import eq_filter as _eq
        eq_str = _eq(color_preset)
        if eq_str:
            fg_scale = f"{fg_scale},{eq_str}"
        fg_pos = f"{fg.x}:{fg.y}"
        if preset.kind == "gradient":
            # PHASE A: two-color animated gradient canvas. The gradients
            # source is INFINITE (no d=) → overlay needs shortest=1.
            canvas = (
                f"gradients=s={target_width}x{target_height}:"
                f"c0={preset.color}:c1={preset.color2}:speed={preset.speed or 0.03}:r={target_fps},"
                f"format=yuv420p[bg];"
                f"[0:v]{fg_scale}[fg];"
                f"[bg][fg]overlay={fg_pos}:shortest=1[v]"
            )
        elif preset.kind == "vignette":
            # PHASE A: solid base + radial darkening.
            canvas = (
                f"color=c={preset.color}:s={target_width}x{target_height}:r={target_fps},"
                f"vignette=PI/4.5,format=yuv420p[bg];"
                f"[0:v]{fg_scale}[fg];"
                f"[bg][fg]overlay={fg_pos}:shortest=1[v]"
            )
        elif preset.kind == "color":
            canvas = (
                f"color=c={preset.color}:s={target_width}x{target_height}:r={target_fps}[bg];"
                f"[0:v]{fg_scale}[fg];"
                # color source is INFINITE → shortest=1, else encode never ends
                f"[bg][fg]overlay={fg_pos}:shortest=1[v]"
            )
        else:
            canvas = (
                "[0:v]split=2[bg_src][fg_src];"
                # Background: cover target (increase) → crop exact → blur.
                f"[bg_src]scale=w={target_width}:h={target_height}:"
                f"force_original_aspect_ratio=increase:"
                f"force_divisible_by=2,"
                f"crop={target_width}:{target_height},"
                f"setsar=1,"
                f"eq=brightness=0.0:contrast=1.1:saturation=1.2,"
                f"gblur=sigma={blur_strength}[bg];"
                # Foreground: EXPLICIT size from compositor. NO AR math here.
                f"[fg_src]{fg_scale}[fg];"
                f"[bg][fg]overlay={fg_pos}:shortest=0[v]"
            )
        filter_complex = canvas

        # Title via TitleRenderer (Phase 7 wiring, audit #31-32): Pillow
        # transparent PNG rendered inside title_box, overlaid — replaces
        # raw drawtext (which was visually raw, no wrap/fit). Brand corner
        # stays drawtext (tiny text, no wrap needed).
        extra_inputs: list[str] = []
        title_used = False
        if title_text:
            try:
                from app.services.rendering.title import TitleRenderer, TitleStyle
                from app.services.overlays.compositor import TemplateSpec
                tbox = TemplateSpec(target_width, target_height).title_box()
                png_bytes = TitleRenderer().render(
                    title_text, tbox, style=TitleStyle(variant="plate"))
                import tempfile as _tmp
                title_png = Path(_tmp.gettempdir()) / f"title_{id(output_path)}.png"
                title_png.write_bytes(png_bytes)
                # Input registry (audit #12): source=0, title=1, decoration=2.
                title_input_idx = len(extra_inputs) // 2 + 1  # 1
                extra_inputs += ["-i", str(title_png)]
                title_chain = (
                    f"[{title_input_idx}:v]format=rgba[t_logo];"
                    f"[v][t_logo]overlay={tbox.x}:{tbox.y}:eof_action=repeat:repeatlast=1[v]"
                )
                filter_complex += ";" + title_chain
                title_used = True
                logger.info("title_rendered_pillow", box=f"{tbox.width}x{tbox.height}@{tbox.x},{tbox.y}")
            except Exception as e:
                logger.warning("title_png_failed_fallback_drawtext", error=str(e)[:150])

        text_filters = ""
        if brand_corner:
            text_filters += "," + _drawtext(
                "ReCut", int(target_height * 0.018),
                x=f"w-text_w-{int(target_width * 0.03)}",
                y=f"{int(target_height * 0.025)}",
                alpha="0.75", borderw=1,
            )
        if text_filters:
            # NOTE: ffmpeg 8.1.2 filtergraph parser rejects intermediate
            # labels ("[v_pre]" style) — chain drawtext directly instead.
            if filter_complex.endswith("[v]"):
                filter_complex = filter_complex[:-3] + text_filters + "[v]"
            else:
                filter_complex += text_filters + "[v]"

        # PHASE B: decorative insert (from DECORATIONS registry) overlaid
        # above the video, below the banner. Animated assets loop forever;
        # enable is the whole clip. Skips silently when not bundled yet.
        if decoration_id:
            from app.services.overlays.templates import DECORATIONS
            dec = DECORATIONS.get(decoration_id)
            if dec is None:
                logger.warning("decoration_unknown_skipped", decoration_id=decoration_id)
            else:
                dec_path = Path(dec.path)
                if not dec_path.exists():
                    logger.warning("decoration_asset_missing_skipped", path=str(dec_path))
                else:
                    from app.services.overlays.compositor import TemplateSpec
                    spec = TemplateSpec(target_width, target_height)
                    dbox = spec.decoration_box(
                        dec.max_width_frac, dec.max_height_frac, dec.anchor,
                    )
                    dec_w, dec_h = _asset_pixel_size(dec_path, dbox.width, dbox.height)
                    # Input registry (audit #12): source=0, title=1 (if used),
                    # decoration = next. No hardcoded indexes.
                    input_idx = 1 + (1 if title_used else 0)
                    dec_kind = (dec.kind or "").lower()
                    is_anim = dec_kind in ("gif", "mp4", "webp")
                    chain = (
                        f"[{input_idx}:v]format=rgba,"
                        f"scale={dec_w}:{dec_h}[dec];"
                        f"[v][dec]overlay={dbox.x}:{dbox.y}:"
                        + ("shortest=1:eof_action=pass[v]" if is_anim
                           else "eof_action=repeat:repeatlast=1[v]")
                    )
                    filter_complex += ";" + chain
                    if is_anim:
                        # Only animated assets loop; a static PNG with
                        # -stream_loop -1 + shortest=0 never terminates.
                        if dec_kind == "gif":
                            extra_inputs += ["-ignore_loop", "0"]
                        extra_inputs += ["-stream_loop", "-1"]
                    extra_inputs += ["-i", str(dec_path)]

        cmd = [
            self._ffmpeg_path,
            "-y",
            "-v", "error",
            "-i", str(source),
        ]
        # TZ Phase 16: video speed via container timestamps (donor
        # ffmpeg-video-bot change_speed pattern); audio atempo comes from
        # AudioProcessor with the SAME factor — synchronized.
        if abs(speed - 1.0) > 0.001:
            i_idx = cmd.index("-i")
            cmd = (cmd[:i_idx]
                   + ["-itsscale", f"{1.0 / speed:.5f}"]
                   + cmd[i_idx:])
        cmd += extra_inputs
        # PART 20-21: audio through AudioProcessor presets.
        from app.services.media.audio import AudioConfig, AudioProcessor
        audio_args, audio_map = AudioProcessor().audio_args(
            AudioConfig(preset=audio_preset, speed=speed),
            audio_bitrate=audio_bitrate)
        # TZ Phase 16: video speed via setpts (donor ffmpeg-video-bot
        # change_speed pattern). Audio atempo comes from AudioProcessor —
        # both use the same factor so they stay synchronized.
        if abs(speed - 1.0) > 0.001:
            filter_complex = filter_complex.replace(
                "[v_pre]", f"[v_pre]setpts=(1/{speed:.5f})*PTS[v_pre]")
        cmd += audio_args
        cmd += [
            "-filter_complex", filter_complex,
            "-map", "[v]",
        ]
        if audio_map:
            cmd += ["-map", audio_map]
        cmd += [
            "-r", str(target_fps),
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-pix_fmt", "yuv420p",
            "-b:v", video_bitrate,
            "-c:a", "aac",
            "-b:a", audio_bitrate,
            "-ac", "2",
            "-movflags", "+faststart",
            str(output_path),
        ]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_seconds,
            )
        except asyncio.TimeoutError as e:
            raise RuntimeError(
                f"FFmpeg make_vertical timed out after {timeout_seconds}s"
            ) from e

        if proc.returncode != 0:
            err = stderr.decode(errors="ignore")[:500]
            logger.error(
                "ffmpeg_make_vertical_failed",
                input=str(input_path), error=err,
            )
            raise RuntimeError(f"FFmpeg make_vertical failed: {err}")

        if not output_path.exists() or output_path.stat().st_size == 0:
            raise RuntimeError(
                f"FFmpeg make_vertical produced empty output at {output_path}"
            )

        logger.info(
            "ffmpeg_make_vertical_done",
            input=str(input_path),
            output=str(output_path),
            target=f"{target_width}x{target_height}",
            bytes=output_path.stat().st_size,
        )
        return output_path

    async def burn_subtitles(
        self,
        video_path: Path,
        ass_path: Path,
        output_path: Path,
        *,
        timeout_seconds: float = 300.0,
    ) -> Path:
        """Hard-burn ASS subtitles into `video_path` -> `output_path`.

        Single re-encode pass with libx264. Output keeps the source
        resolution / fps, gets H.264 + AAC.
        """
        if not video_path.exists():
            raise FileNotFoundError(f"Video not found: {video_path}")
        if not ass_path.exists():
            raise FileNotFoundError(f"ASS file not found: {ass_path}")
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # ass filter — ':force_style' lets us override style on the fly.
        # Build the filter string outside f-string to avoid the backslash
        # restriction on Python 3.11.
        ass_escaped = str(ass_path).replace(":", "\\:")
        vf = "ass=" + ass_escaped

        cmd = [
            self._ffmpeg_path,
            "-y",
            "-v", "error",
            "-i", str(video_path),
            "-vf", vf,
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-pix_fmt", "yuv420p",
            "-c:a", "copy",
            "-movflags", "+faststart",
            str(output_path),
        ]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_seconds,
            )
        except asyncio.TimeoutError as e:
            raise RuntimeError(
                f"FFmpeg burn_subtitles timed out after {timeout_seconds}s"
            ) from e

        if proc.returncode != 0:
            err = stderr.decode(errors="ignore")[:500]
            logger.error(
                "ffmpeg_burn_subtitles_failed",
                input=str(video_path),
                error=err,
            )
            raise RuntimeError(f"FFmpeg burn_subtitles failed: {err}")

        if not output_path.exists() or output_path.stat().st_size == 0:
            raise RuntimeError("FFmpeg burn_subtitles produced empty output")

        logger.info(
            "ffmpeg_burn_subtitles_done",
            input=str(video_path),
            output=str(output_path),
            bytes=output_path.stat().st_size,
        )
        return output_path

    async def detect_black_bars(
        self,
        video_path: Path,
        *,
        sample_frames: int = 15,
        min_agreement: float = 0.7,
        timeout_seconds: float = 90.0,
    ) -> tuple[int, int, int, int] | None:
        """Detect stable letterbox/pillarbox black bars via cropdetect.

        Samples the first `sample_frames` frames; the crop must agree on
        (w, h) for >= min_agreement of samples AND on (x, y). Returns
        (crop_w, crop_h, crop_x, crop_y) or None.

        Guards: crop must actually trim at least one dimension and must
        never remove more than half of either dimension (protects against
        destroying real content).
        """
        if not video_path.exists():
            raise FileNotFoundError(f"Video not found: {video_path}")

        # PART 11: sample frames spread across the WHOLE video (10-90%),
        # not just the first sequential frames. select='not(mod(n,K))'
        # picks every K-th decoded frame; K spreads sample_frames evenly
        # over the estimated total frame count.
        # NOTE: self.probe() returns a raw JSON dict (not VideoProbeResult).
        info = await self.probe(video_path)
        vstream = next(
            (s for s in (info.get("streams") or []) if s.get("codec_type") == "video"), {},
        )
        fps = 30.0
        try:
            fr = vstream.get("avg_frame_rate") or vstream.get("r_frame_rate") or "30/1"
            num, _, den = fr.partition("/")
            fps = float(num) / max(float(den or 1), 1)
        except (ValueError, ZeroDivisionError):
            pass
        fmt_dur = 0.0
        try:
            fmt_dur = float((info.get("format") or {}).get("duration") or 0)
        except (ValueError, TypeError):
            pass
        total_frames = max(1, int(fps * max(fmt_dur, 0.1)))
        step = max(1, total_frames // max(sample_frames, 1))
        select_expr = f"select='not(mod(n\\,{step}))',cropdetect=limit=24:round=2:reset=0"

        cmd = [
            self._ffmpeg_path,
            "-v", "info",
            "-i", str(video_path),
            "-vf", select_expr,
            "-frames:v", str(sample_frames),
            "-f", "null", "-",
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
        except asyncio.TimeoutError as e:
            proc.kill()
            raise RuntimeError("cropdetect timed out") from e

        import re
        crops: list[tuple[int, int, int, int]] = []
        for line in stderr.decode(errors="ignore").splitlines():
            m = re.search(r"crop=(\d+):(\d+):(\d+):(\d+)", line)
            if m:
                crops.append(tuple(int(g) for g in m.groups()))
        if not crops:
            return None

        # Source dimensions from the probe (raw JSON dict, not dataclass).
        src_w = int(vstream.get("width") or 0)
        src_h = int(vstream.get("height") or 0)

        n = len(crops)
        mode_wh: tuple[int, int] | None = None
        best_count = 0
        for wh in set((c[0], c[1]) for c in crops):
            cnt = sum(1 for c in crops if (c[0], c[1]) == wh)
            if cnt > best_count:
                best_count = cnt
                mode_wh = wh
        if mode_wh is None or best_count / n < min_agreement:
            logger.info("cropdetect_unstable", samples=n, agreement=best_count / n)
            return None

        matching = [c for c in crops if (c[0], c[1]) == mode_wh]
        x_counts: dict[int, int] = {}
        y_counts: dict[int, int] = {}
        for c in matching:
            x_counts[c[2]] = x_counts.get(c[2], 0) + 1
            y_counts[c[3]] = y_counts.get(c[3], 0) + 1
        mode_x = max(x_counts, key=x_counts.get)
        mode_y = max(y_counts, key=y_counts.get)
        if x_counts[mode_x] / len(matching) < min_agreement or y_counts[mode_y] / len(matching) < min_agreement:
            return None

        w, h = mode_wh
        x, y = mode_x, mode_y
        # Guards.
        if w >= src_w and h >= src_h:
            return None  # nothing to trim
        if src_w and w < src_w * 0.5:
            return None
        if src_h and h < src_h * 0.5:
            return None
        return (w, h, x, y)

    async def _run_crop_pass(
        self, input_path: Path, output_path: Path,
        w: int, h: int, x: int, y: int, timeout: float = 300.0,
    ) -> None:
        """Pre-crop real content (re-encode; crop filter can't stream-copy)."""
        cmd = [
            self._ffmpeg_path, "-y", "-v", "error",
            "-i", str(input_path),
            "-vf", f"crop={w}:{h}:{x}:{y},setsar=1",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "copy",
            "-movflags", "+faststart",
            str(output_path),
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError as e:
            proc.kill()
            raise RuntimeError("crop pass timed out") from e
        if proc.returncode != 0:
            raise RuntimeError(f"crop pass failed: {stderr.decode(errors='ignore')[:300]}")

    async def burn_cta(
        self,
        video_path: Path,
        cta_path: Path,
        output_path: Path,
        *,
        position: str,
        margin: int,
        start_seconds: float,
        end_seconds: float,
        size_preset: str = "medium",
        overlay_type: str = "png",
        overlay_is_animated: bool = False,
        timeout_seconds: float = 300.0,
    ) -> Path:
        """Overlay a banner (static or animated) over a window of the video.

        Safe-area sizing (audit #6): banner max width by size preset
        (small 28% / medium 33% / large 38% of frame width), max height
        15% of frame height. Never full-screen, never upscaled.
        Positioning from actual main_w/main_h/overlay_w/overlay_h.

        overlay_type: png/webp — static image input; gif/mp4 — animated
        overlay, looped for the duration of the enable window
        (-ignore_loop 0 + shortest=1 on the overlay so the banner
        disappears when the window ends).
        """
        if not video_path.exists():
            raise FileNotFoundError(f"Video not found: {video_path}")
        if not cta_path.exists():
            raise FileNotFoundError(f"CTA asset not found: {cta_path}")
        output_path.parent.mkdir(parents=True, exist_ok=True)

        x_expr, y_expr = _cta_position_exprs(position, margin)
        # --- Safe-area banner sizing (computed in Python; overlay only) ---
        # max width  = 85% of video width (or full width minus side margins
        #              for full_width_bottom)
        # max height = 15% of video height
        # scale preserves aspect ratio, never upscales, never crops.
        banner_out_w = 0
        banner_out_h = 0
        video_w = 0
        video_h = 0
        banner_in_w = 0
        banner_in_h = 0
        try:
            video_info = await self.probe(video_path)
            vstreams = video_info.get("streams") or []
            vstream = next((s for s in vstreams if s.get("codec_type") == "video"), {})
            video_w = int(vstream.get("width") or 0)
            video_h = int(vstream.get("height") or 0)

            banner_info = await self.probe(cta_path)
            bstreams = banner_info.get("streams") or []
            bstream = next((s for s in bstreams if s.get("codec_type") == "video"), {})
            banner_in_w = int(bstream.get("width") or 0)
            banner_in_h = int(bstream.get("height") or 0)
        except Exception as e:
            logger.warning("cta_probe_failed", error=str(e)[:200])

        if video_w > 0 and video_h > 0 and banner_in_w > 0 and banner_in_h > 0:
            # Effective margin: caller value if given, else ~4% of frame
            # height (audit #6: bottom margin 3-5%).
            margin_px = margin if margin > 0 else int(video_h * 0.04)
            side_margin = int(video_w * 0.04)

            # PHASE 1: size preset = TARGET VISUAL WIDTH (not just a cap).
            # Root cause of "small/medium/large look identical": old code
            # scaled with min(1.0, ...) so a small asset never upscaled and
            # every preset rendered at the asset's native size.
            target_frac = {"small": 0.24, "medium": 0.34, "large": 0.46}.get(size_preset, 0.34)
            if position == "full_width_bottom":
                target_w = video_w - 2 * side_margin
            else:
                target_w = video_w * target_frac
            max_h = video_h * 0.18  # safety limit (was 15%)

            scale = target_w / max(banner_in_w, 1)
            # Cap by max height (preserve aspect, never crop/stretch).
            if banner_in_h * scale > max_h:
                scale = max_h / banner_in_h
            if scale < 1.0:
                logger.warning(
                    "cta_banner_too_large_auto_scaled",
                    input_width=banner_in_w, input_height=banner_in_h,
                    scale=round(scale, 3),
                    max_height=int(max_h),
                )
            banner_out_w = max(2, int(banner_in_w * scale) // 2 * 2)
            banner_out_h = max(2, int(banner_in_h * scale) // 2 * 2)

            # Re-derive x/y with the effective margin (margin_px may differ
            # from the raw `margin` argument when caller passed 0/negative)
            # BEFORE logging so the debug log shows final coordinates.
            x_expr, y_expr = _cta_position_exprs(position, margin_px)
            logger.info(
                "cta_banner_scaling",
                input_banner_width=banner_in_w,
                input_banner_height=banner_in_h,
                output_banner_width=banner_out_w,
                output_banner_height=banner_out_h,
                video_width=video_w,
                video_height=video_h,
                banner_position=position,
                margin_px=margin_px,
                x_expression=x_expr,
                y_expression=y_expr,
                start_seconds=start_seconds,
                end_seconds=end_seconds,
            )

        is_animated = overlay_is_animated or overlay_type in ("gif", "mp4")
        # Animated overlays loop for the whole window:
        # GIF: -ignore_loop 0; MP4/GIF: -stream_loop -1.
        if banner_out_w > 0:
            cta_chain = f"[1:v]format=rgba,scale={banner_out_w}:{banner_out_h}[cta];"
        else:
            # Probe failed — don't scale, use native overlay size.
            cta_chain = "[1:v]format=rgba[cta];"
        overlay_kwargs = (
            "shortest=0:eof_action=repeat:repeatlast=1"
            if not is_animated
            # animated: banner stream ends with the enable window
            else "shortest=1:eof_action=pass"
        )
        filter_expr = (
            cta_chain +
            f"[0:v][cta]overlay="
            f"x={x_expr}:y={y_expr}:"
            f"enable='between(t,%g,%g)':"
            f"{overlay_kwargs}[v]"
        ) % (start_seconds, end_seconds)

        cmd = [
            self._ffmpeg_path,
            "-y",
            "-v", "error",
            "-i", str(video_path),
        ]
        if is_animated:
            # Loop the overlay stream forever; enable window bounds it.
            # Input options must precede THEIR OWN -i (the overlay input).
            if overlay_type == "gif":
                cmd += ["-ignore_loop", "0"]
            cmd += ["-stream_loop", "-1"]
        cmd += [
            "-i", str(cta_path),
            "-filter_complex", filter_expr,
            "-map", "[v]",
            "-map", "0:a?",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-pix_fmt", "yuv420p",
            "-c:a", "copy",
            "-movflags", "+faststart",
            str(output_path),
        ]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_seconds,
            )
        except asyncio.TimeoutError as e:
            raise RuntimeError(
                f"FFmpeg burn_cta timed out after {timeout_seconds}s"
            ) from e

        if proc.returncode != 0:
            err = stderr.decode(errors="ignore")[:500]
            logger.error(
                "ffmpeg_burn_cta_failed",
                input=str(video_path), error=err,
            )
            raise RuntimeError(f"FFmpeg burn_cta failed: {err}")

        if not output_path.exists() or output_path.stat().st_size == 0:
            raise RuntimeError("FFmpeg burn_cta produced empty output")

        logger.info(
            "ffmpeg_burn_cta_done",
            input=str(video_path),
            output=str(output_path),
            bytes=output_path.stat().st_size,
        )
        return output_path

    async def finalize_export(
        self,
        video_path: Path,
        output_path: Path,
        *,
        target_lufs: float = -16.0,
        timeout_seconds: float = 300.0,
    ) -> Path:
        """Final clean export — strips source metadata / chapters / paths.

        Uses loudnorm (EBU R128) two-pass for sane loudness across all
        clips, and `-map_metadata -1 -map_chapters -1` to wipe every
        piece of metadata that might leak source filename / paths.

        Note: this is a privacy / portability pass, not an anti-detection
        pass — we don't do pixel mods, frame jitter, or any other
        tricks that aim to evade platform fingerprinting.
        """
        if not video_path.exists():
            raise FileNotFoundError(f"Video not found: {video_path}")
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Pass 1: measure loudness.
        measure_cmd = [
            self._ffmpeg_path, "-v", "info",
            "-i", str(video_path),
            "-af", f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11:print_format=json",
            "-f", "null", "-",
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *measure_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            logger.warning("loudnorm_measure_timeout_fallback_copy")
            # Fall back to plain remux-with-metadata-strip.
            return await self._strip_only(video_path, output_path)

        import json
        # loudnorm prints JSON to stderr. Find the block containing "input_i"
        stderr_text = stderr.decode(errors="ignore")
        idx = stderr_text.find('"input_i"')
        if idx == -1:
            logger.warning("loudnorm_parse_failed_fallback_strip")
            return await self._strip_only(video_path, output_path)
        # Find the surrounding braces (loudnorm JSON is flat, single level)
        brace_start = stderr_text.rfind('{', 0, idx)
        brace_end = stderr_text.find('}', idx)
        if brace_start == -1 or brace_end == -1:
            logger.warning("loudnorm_parse_failed_fallback_strip")
            return await self._strip_only(video_path, output_path)
        measured = json.loads(stderr_text[brace_start:brace_end + 1])

        # Pass 2: apply loudnorm + strip metadata.
        apply_cmd = [
            self._ffmpeg_path, "-y", "-v", "error",
            "-i", str(video_path),
            "-af", (
                f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11:"
                f"measured_I={measured.get('input_i', target_lufs)}:"
                f"measured_TP={measured.get('input_tp', -1.5)}:"
                f"measured_LRA={measured.get('input_lra', 11)}:"
                f"measured_thresh={measured.get('input_thresh', -99)}:"
                f"offset={measured.get('target_offset', 0)}:linear=true:print_format=summary"
            ),
            "-c:v", "copy",
            "-c:a", "aac",
            "-b:a", "128k",
            "-map_metadata", "-1",     # strip ALL metadata
            "-map_chapters", "-1",    # strip chapter info
            "-metadata", "comment=",  # clear comment
            "-metadata", "title=",    # clear title
            "-movflags", "+faststart",
            str(output_path),
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *apply_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_seconds,
            )
        except asyncio.TimeoutError as e:
            raise RuntimeError(
                f"FFmpeg finalize timed out after {timeout_seconds}s"
            ) from e

        if proc.returncode != 0:
            err = stderr.decode(errors="ignore")[:500]
            logger.warning("loudnorm_apply_failed_fallback_strip", error=err)
            return await self._strip_only(video_path, output_path)

        if not output_path.exists() or output_path.stat().st_size == 0:
            return await self._strip_only(video_path, output_path)

        logger.info(
            "ffmpeg_finalize_done",
            input=str(video_path),
            output=str(output_path),
            bytes=output_path.stat().st_size,
        )
        return output_path

    async def _strip_only(
        self,
        video_path: Path,
        output_path: Path,
        *,
        timeout_seconds: float = 60.0,
    ) -> Path:
        """Fallback: re-mux while stripping every piece of metadata."""
        output_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            self._ffmpeg_path, "-y", "-v", "error",
            "-i", str(video_path),
            "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
            "-map_metadata", "-1",
            "-map_chapters", "-1",
            "-metadata", "comment=",
            "-metadata", "title=",
            "-movflags", "+faststart",
            str(output_path),
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout_seconds,
        )
        if proc.returncode != 0:
            err = stderr.decode(errors="ignore")[:500]
            raise RuntimeError(f"FFmpeg strip failed: {err}")
        return output_path

    async def probe(self, filepath: Path) -> dict:
        """Probe media file for info (duration, format, etc.)."""
        if not filepath.exists():
            raise FileNotFoundError(f"File not found: {filepath}")

        cmd = [
            str(Path(self._ffmpeg_path).with_name("ffprobe.exe"))
            if Path(self._ffmpeg_path).with_name("ffprobe.exe").exists()
            else str(Path(self._ffmpeg_path).with_name("ffprobe")),
            "-v", "error",
            "-show_entries", "format=duration,bit_rate,format_name:stream=codec_type,codec_name,sample_rate,channels,width,height,sample_aspect_ratio,display_aspect_ratio",
            "-of", "json",
            str(filepath),
        ]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            logger.error("ffprobe_failed", file=str(filepath), error=stderr.decode())
            raise RuntimeError(f"ffprobe failed: {stderr.decode()}")

        import json
        return json.loads(stdout.decode())

    async def convert_audio(
        self,
        input_path: Path,
        output_path: Path,
        format: str = "mp3",
        bitrate: str = "128k",
        input_format: Optional[str] = None,
        sample_rate: Optional[int] = None,
        channels: Optional[int] = None,
    ) -> Path:
        """Convert audio to target format.

        For raw PCM input, set input_format="s16le" plus sample_rate and channels
        so ffmpeg can interpret the bytes correctly.
        """
        if not input_path.exists():
            raise FileNotFoundError(f"Input not found: {input_path}")

        output_path.parent.mkdir(parents=True, exist_ok=True)

        cmd = [
            self._ffmpeg_path,
            "-y",  # overwrite
        ]
        # Raw input hints (PCM only)
        if input_format:
            cmd += ["-f", input_format]
        if sample_rate:
            cmd += ["-ar", str(sample_rate)]
        if channels:
            cmd += ["-ac", str(channels)]
        cmd += [
            "-i", str(input_path),
            # WAV: just remux into container (PCM stays PCM, no re-encode)
            "-c:a", "copy" if format == "wav" else ("libmp3lame" if format == "mp3" else "copy"),
            "-b:a", bitrate,
            str(output_path),
        ]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            logger.error(
                "ffmpeg_convert_failed",
                input=str(input_path),
                error=stderr.decode()[:500],
            )
            raise RuntimeError(f"FFmpeg conversion failed: {stderr.decode()}")

        logger.info("ffmpeg_convert_done", input=str(input_path), output=str(output_path))
        return output_path

    async def normalize_audio(
        self,
        input_path: Path,
        output_path: Path,
        target_lufs: float = -16.0,
    ) -> Path:
        """Normalize audio loudness (EBU R128)."""
        if not input_path.exists():
            raise FileNotFoundError(f"Input not found: {input_path}")

        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Two-pass normalization: measure then apply
        # Pass 1: measure
        cmd_measure = [
            self._ffmpeg_path,
            "-i", str(input_path),
            "-af", f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11:print_format=json",
            "-f", "null", "-",
        ]

        proc = await asyncio.create_subprocess_exec(
            *cmd_measure,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        # Parse measured values from stderr (FFmpeg outputs JSON to stderr)
        import json
        import re
        json_match = re.search(r"\{.*\}", stderr.decode(), re.DOTALL)
        if not json_match:
            logger.warning("loudnorm_measure_failed", stderr=stderr.decode()[:500])
            # Fallback: just copy
            return await self.convert_audio(input_path, output_path)

        measured = json.loads(json_match.group())

        # Pass 2: apply
        cmd_apply = [
            self._ffmpeg_path,
            "-y",
            "-i", str(input_path),
            "-af", (
                f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11:"
                f"measured_I={measured.get('input_i', target_lufs)}:"
                f"measured_TP={measured.get('input_tp', -1.5)}:"
                f"measured_LRA={measured.get('input_lra', 11)}:"
                f"measured_thresh={measured.get('input_thresh', -99)}:"
                f"offset={measured.get('target_offset', 0)}:"
                f"linear=true:print_format=summary"
            ),
            "-c:a", "libmp3lame",
            "-b:a", "128k",
            str(output_path),
        ]

        proc = await asyncio.create_subprocess_exec(
            *cmd_apply,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            logger.error("loudnorm_apply_failed", error=stderr.decode())
            raise RuntimeError(f"Loudnorm failed: {stderr.decode()}")

        logger.info("audio_normalized", input=str(input_path), output=str(output_path))
        return output_path

    async def ensure_telegram_compatible(self, input_path: Path, output_path: Path) -> Path:
        """
        Ensure audio is compatible with Telegram (MP3, <= 50MB, proper headers).
        Currently just converts to MP3 128k if needed.
        """
        # Probe first
        info = await self.probe(input_path)

        # Check if already MP3
        audio_streams = [s for s in info.get("streams", []) if s.get("codec_type") == "audio"]
        if audio_streams and audio_streams[0].get("codec_name") == "mp3":
            # Check size
            size_mb = input_path.stat().st_size / (1024 * 1024)
            if size_mb <= 50:
                # Already compatible, just copy
                import shutil
                shutil.copy2(input_path, output_path)
                return output_path

        # Convert
        return await self.convert_audio(input_path, output_path, format="mp3", bitrate="128k")


# Global instance
media_service = MediaService()


def get_media_service() -> MediaService:
    return media_service


# ---------------------------------------------------------------------------
# CTA positioning helpers (used by burn_cta filtergraph)
# ---------------------------------------------------------------------------


def _cta_position_exprs(position: str, margin: int) -> tuple[str, str]:
    """Return (x_expr, y_expr) FFmpeg overlay expressions.

    Uses FFmpeg built-in variables: main_w, main_h, overlay_w, overlay_h.
    Positions the CTA relative to actual dimensions, not hardcoded pixels.
    """
    if position == "top":
        return (f"(main_w-overlay_w)/2", f"{margin}")
    elif position in ("bottom", "full_width_bottom"):
        # full_width_bottom: banner already scaled to (W - 2*side_margin),
        # so centering equals the side-margin placement.
        return (f"(main_w-overlay_w)/2", f"main_h-overlay_h-{margin}")
    elif position == "top_left":
        return (f"{margin}", f"{margin}")
    elif position == "top_right":
        return (f"main_w-overlay_w-{margin}", f"{margin}")
    elif position == "bottom_left":
        return (f"{margin}", f"main_h-overlay_h-{margin}")
    elif position == "bottom_right":
        return (f"main_w-overlay_w-{margin}", f"main_h-overlay_h-{margin}")
    else:
        # Default: center bottom
        return (f"(main_w-overlay_w)/2", f"main_h-overlay_h-{margin}")