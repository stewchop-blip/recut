"""Transparent, subdued ReCut watermark; no card or promotional tagline."""
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont


def render_brand(width: int) -> bytes:
    scale = 3
    w = max(1, width) * scale
    h = max(1, round(width * 0.30)) * scale
    image = Image.new("RGBA", (w, h))
    draw = ImageDraw.Draw(image)
    pad = max(1, round(w * 0.015))
    for text, size, y, bold in (("ReCut", .14, .015, True),
                               ("t.me/contentcutbot", .085, .19, False)):
        font = ImageFont.truetype("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf",
                                  max(1, round(w * size)))
        x = w - pad - draw.textlength(text, font=font)
        draw.text((x, round(w*y)), text, font=font, anchor="lt",
                  fill=(255, 255, 255, 185), stroke_width=max(1, round(w*.003)),
                  stroke_fill=(0, 0, 0, 100))
    image = image.resize((width, max(1, round(width * .30))), Image.Resampling.LANCZOS)
    output = BytesIO()
    image.save(output, "PNG")
    return output.getvalue()
