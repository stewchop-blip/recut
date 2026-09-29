"""Sparse, low-opacity twinkles for the shared video composition pass.

This is a visual finish, not a promise of recommendation eligibility. No
additional input, encode pass, external asset or user-facing setting is needed.
"""
from __future__ import annotations

from typing import Callable

# Normalized positions inside the foreground. Different phases and slow periods
# keep the points from flashing together. Keep the layer sparse and subdued.
_POINTS = (
    (0.13, 0.19, 4.8, 0.4), (0.72, 0.14, 6.7, 2.1),
    (0.38, 0.28, 5.6, 4.3), (0.87, 0.35, 7.2, 1.3),
    (0.22, 0.43, 6.1, 3.7), (0.61, 0.48, 5.3, 5.2),
    (0.09, 0.63, 7.6, 2.8), (0.79, 0.59, 6.4, 0.9),
    (0.43, 0.71, 5.9, 4.8), (0.68, 0.82, 7.1, 3.2),
)


def twinkle_filters(x: int, y: int, width: int, height: int,
                    drawtext: Callable[..., str]) -> str:
    """Return a comma-separated filter chain, contained in the video box.

    The existing drawtext helper owns font resolution/escaping. Using ASCII '*'
    avoids missing emoji glyphs on the production image. Peak opacity is 6.5%;
    the smooth 4.8–7.6 second cycles have no abrupt on/off transitions.
    """
    size = max(6, round(min(width, height) * 0.014))
    filters = []
    for index, (px, py, period, phase) in enumerate(_POINTS):
        point_size = max(4, size - index % 3)
        fx = x + round(px * max(0, width - point_size))
        fy = y + round(py * max(0, height - point_size))
        base = drawtext("*", point_size, x=str(fx), y=str(fy),
                        alpha="0.065", borderw=0)
        envelope = f"pow((1+sin(2*PI*t/{period:.1f}+{phase:.1f}))/2,3)"
        filters.append(f"{base}:alpha='{envelope}'")
    return ",".join(filters)
