"""Small, legible ReCut signature, rendered at 2x for smooth edges."""
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont


def render_brand(width: int) -> bytes:
    scale = 2
    w = max(120, width) * scale
    h = round(w * 0.39)
    image = Image.new('RGBA', (w, h))
    draw = ImageDraw.Draw(image)
    unit = w / 380
    draw.rounded_rectangle((0, 0, w-1, h-1), radius=round(22*unit),
                           fill=(18, 24, 38, 238), outline=(91, 112, 146, 160), width=scale)
    pad = round(20*unit)
    icon = round(39*unit)
    draw.rounded_rectangle((pad, pad, pad+icon, pad+icon), radius=round(11*unit),
                           fill=(159, 229, 93, 255))
    draw.polygon([(pad+icon*.38, pad+icon*.24), (pad+icon*.38, pad+icon*.76),
                  (pad+icon*.76, pad+icon*.50)], fill=(18, 24, 38, 255))

    def line(text, x, y, size, color, bold=False):
        name = 'DejaVuSans-Bold.ttf' if bold else 'DejaVuSans.ttf'
        font = ImageFont.truetype(name, max(1, round(size*unit)))
        draw.text((x, round(y*unit)), text, font=font, fill=color, anchor='lt')

    line('ReCut', pad+icon+round(12*unit), 23, 32, 'white', True)
    line('Клип за пару кликов', pad, 71, 22, (214, 224, 239, 255))
    line('t.me/contentcutbot', pad, 107, 24, (159, 229, 93, 255), True)
    image = image.resize((width, round(width*.39)), Image.Resampling.LANCZOS)
    output = BytesIO()
    image.save(output, 'PNG')
    return output.getvalue()
