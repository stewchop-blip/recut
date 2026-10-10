"""Small transparent ReCut signature with a readable outline."""
from io import BytesIO
from PIL import Image, ImageDraw, ImageFont


def render_brand(width: int) -> bytes:
    width = max(24, width)
    scale = 3
    height = round(width * .32)
    image = Image.new("RGBA", (width*scale, height*scale))
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype("DejaVuSans-Bold.ttf", round(width*scale*.24))
    draw.text((width*scale//2, height*scale//2), "ReCut", font=font, anchor="mm",
              fill=(255,255,255,220), stroke_width=max(1, round(width*scale*.008)),
              stroke_fill=(0,0,0,190))
    image = image.resize((width, height), Image.Resampling.LANCZOS)
    output = BytesIO()
    image.save(output, "PNG")
    return output.getvalue()
