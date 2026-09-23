"""Default CTA asset generation.

We never want a missing CTA file to crash a job. If the configured
asset path is missing, we generate a sane placeholder PNG with Pillow
(simple white text on transparent background) and save it next to the
job's working directory.
"""
from __future__ import annotations

from pathlib import Path
from typing import Tuple


def generate_default_cta(
    output_path: Path,
    *,
    text: str = "Recut",
    width: int = 720,
    height: int = 200,
) -> Path:
    """Write a simple PNG with `text` centered, white on transparent."""
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Pick a default font that ships with PIL.
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", size=72)
    except OSError:
        font = ImageFont.load_default()

    # Center text.
    bbox = draw.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    x = (width - text_w) // 2
    y = (height - text_h) // 2

    # Draw a subtle black drop shadow for legibility on light backgrounds.
    draw.text((x + 3, y + 3), text, fill=(0, 0, 0, 220), font=font)
    # Draw the white text on top.
    draw.text((x, y), text, fill=(255, 255, 255, 255), font=font)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(output_path, "PNG")
    return output_path


def ensure_cta_asset(
    configured_path: str,
    fallback_dir: Path,
) -> Tuple[Path, bool]:
    """Return (path, was_generated).

    Priority:
    1. `configured_path` (user upload — highest priority, overrides default)
    2. Statically-shipped app/assets/cta/banner.png (bundled in repo)
    3. Auto-generated placeholder in fallback_dir
    """
    if configured_path:
        p = Path(configured_path)
        if p.exists():
            return p, False

    # Static repo asset — always available, works on Railway ephemeral
    import os
    repo_asset = Path(os.path.join(
        os.path.dirname(__file__), "..", "..", "assets", "cta", "banner.png"
    )).resolve()
    if repo_asset.exists():
        return repo_asset, False

    out = fallback_dir / "cta_default.png"
    if not out.exists():
        generate_default_cta(out)
    return out, True