"""Optional saved appearance stages, using the existing media and speech services."""
from pathlib import Path
from types import SimpleNamespace

from app.core.logging import get_logger
from app.services.media.ffmpeg_runner import FFmpegRunner, FFmpegError
from app.services.appearance import validate_image

logger = get_logger(__name__)


async def add_subtitles(media, video: Path, job_dir: Path, *, style: str, language: str) -> Path:
    from app.pipeline.transcriber import Transcriber
    from app.services.subtitles.ass import AssSubtitleBuilder
    from app.services.subtitles.base import SubtitleBuildRequest
    wav = job_dir / 'speech.wav'
    await media.extract_audio(video, wav, sample_rate=16000, channels=1)
    transcript = await Transcriber().transcribe(wav, language=language)
    builder = AssSubtitleBuilder()
    result = builder.build(SubtitleBuildRequest(segments=transcript.segments,
        clip_start=0, clip_end=transcript.duration_seconds))
    try:
        if not result.output_path:
            return video
        ass_path = job_dir / 'subtitles.ass'
        # Use rendered output dimensions, keep safe margins proportional.
        metadata = await media.probe(video)
        stream = next(stream for stream in metadata['streams'] if stream.get('codec_type') == 'video')
        width, height = int(stream['width']), int(stream['height'])
        config = SimpleNamespace(output_width=width, output_height=height,
            subtitle_safe_left_px=round(width * .07), subtitle_safe_right_px=round(width * .07),
            subtitle_safe_bottom_px=round(height * .18))
        text = builder._render_ass(list(result.phrases), config)
        text = text.replace('Arial,56,', f'Arial,{round(width * (.065 if style == "large" else .052))},')
        ass_path.write_text(text, encoding='utf-8')
        output = job_dir / 'with_subtitles.mp4'
        await media.burn_subtitles(video, ass_path, output)
        return output
    finally:
        if result.output_path:
            Path(result.output_path).unlink(missing_ok=True)


async def add_corner_asset(video: Path, asset: Path, output: Path, *, width: int, height: int,
                           logo=False, avoid_top=False) -> Path:
    validate_image(asset)
    fraction = .14 if logo else .18
    pixels = max(24, round(width * fraction))
    margin = max(12, round(width * .03))
    x = str(margin) if logo else f'W-w-{margin}'
    y = f'H-h-{max(margin, round(height*.12))}' if avoid_top else str(max(margin, round(height*.04)))
    args = ['-i', str(video), '-i', str(asset), '-filter_complex',
        f'[1:v]scale={pixels}:{max(20, round(height*.07))}:force_original_aspect_ratio=decrease,format=rgba[asset];'
        f'[0:v][asset]overlay=x={x}:y={y}:eof_action=repeat:repeatlast=1:format=auto[v]',
        '-map', '[v]', '-map', '0:a?', '-c:v', 'libx264', '-preset', 'fast', '-crf', '20',
        '-pix_fmt', 'yuv420p', '-c:a', 'copy', '-movflags', '+faststart', str(output)]
    result = await FFmpegRunner().run(args, output_path=output, timeout_seconds=300)
    if not result.success:
        raise FFmpegError('APPEARANCE_OVERLAY_FAILED')
    return output
