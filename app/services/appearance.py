"""Server-side appearance policy and bounded, content-verified image assets."""
import asyncio
import warnings
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

MAX_ASSET_BYTES = 10 * 1024 * 1024
MAX_ASSET_PIXELS = 16_000_000
MAX_ASSET_SIDE = 4096
branding_required: ContextVar[bool | None] = ContextVar('branding_required', default=None)


def is_premium(settings, now=None) -> bool:
    expires = getattr(settings, 'premium_until', None)
    if not isinstance(expires, datetime):
        return False
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return expires > (now or datetime.now(timezone.utc))


def effective_branding(settings) -> bool:
    return not is_premium(settings) or bool(getattr(settings, 'recut_branding', True))


def validate_image(path: Path, *, allow_animation=False) -> tuple[str, bool]:
    if not 0 < path.stat().st_size <= MAX_ASSET_BYTES:
        raise ValueError('ASSET_SIZE')
    with warnings.catch_warnings():
        warnings.simplefilter('error', Image.DecompressionBombWarning)
        with Image.open(path) as image:
            fmt = {'PNG': 'png', 'JPEG': 'jpeg', 'WEBP': 'webp', 'GIF': 'gif'}.get(image.format)
            if not fmt:
                raise ValueError('ASSET_FORMAT')
            w, h = image.size
            frames = getattr(image, 'n_frames', 1)
            if not (0 < w <= MAX_ASSET_SIDE and 0 < h <= MAX_ASSET_SIDE) or w*h > MAX_ASSET_PIXELS:
                raise ValueError('ASSET_DIMENSIONS')
            if frames > 300 or w*h*frames > 100_000_000 or frames > 1 and not allow_animation:
                raise ValueError('ASSET_ANIMATION')
            image.verify()
        # verify() validates structure; decode validates the actual image data.
        with Image.open(path) as image:
            duration_ms = 0
            for index in range(frames):
                image.seek(index)
                duration_ms += image.info.get('duration', 0)
                if duration_ms > 30_000:
                    raise ValueError('ASSET_DURATION')
                image.load()
    return fmt, frames > 1


async def download_asset(bot, file_id: str, destination: Path) -> Path:
    file = await bot.get_file(file_id)
    if file.file_size and file.file_size > MAX_ASSET_BYTES:
        raise ValueError('ASSET_SIZE')
    try:
        await asyncio.wait_for(bot.download_file(file.file_path, destination=destination), 60)
        if not 0 < destination.stat().st_size <= MAX_ASSET_BYTES:
            raise ValueError('ASSET_SIZE')
        return destination
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


def output_geometry(settings, defaults) -> tuple[int, int, str]:
    quality = getattr(settings, 'output_quality', 'standard')
    width = defaults.output_width if quality != 'compact' else min(720, defaults.output_width)
    mode = getattr(settings, 'output_mode', 'universal_9_16')
    ratio = {'square': 1, 'portrait_4_5': 5/4}.get(mode, 16/9)
    return width // 2 * 2, round(width * ratio / 2) * 2, ('3M' if quality == 'compact' else defaults.output_video_bitrate)
