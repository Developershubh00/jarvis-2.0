"""The tutor pointer's plain-Python side: the arrow's shape, where the label bubble goes, the glow and
the glide. The AppKit side (overlay.py) only draws what this describes. Coordinates are points from
the top-left of the main display (y grows downwards)."""
from __future__ import annotations

import math

MOVE_SECONDS = 0.55
EDGE = 8.0
# Classic arrow cursor outline with its tip at (0, 0).
ARROW = [(0, 0), (0, 25), (6.5, 19.5), (11, 29.5), (15.5, 27.5), (11, 18), (19, 18)]


def arrow_points(x: float, y: float) -> list[tuple[float, float]]:
    return [(x + px, y + py) for px, py in ARROW]


def glow_rings(pulse: float) -> list[tuple[float, float]]:
    """(radius, alpha) of the soft glow under the tip; it breathes with ``pulse`` (seconds)."""
    p = 0.5 + 0.5 * math.sin(pulse * 4.0)
    return [(30.0 + 6.0 * p, 0.10), (20.0 + 4.0 * p, 0.16), (11.0, 0.30)]


def bubble_layout(x: float, y: float, text_w: float, text_h: float, view_w: float, view_h: float):
    """Where the label bubble and its text go, as (x, y, w, h) rects: below-right of the tip, flipped to
    the other side near the right or bottom edge, and always fully on screen."""
    bw, bh = text_w + 26.0, text_h + 16.0
    bx, by = x + 26.0, y + 22.0
    if bx + bw > view_w - EDGE:
        bx = x - bw - 14.0
    if by + bh > view_h - EDGE:
        by = y - bh - 10.0
    bx, by = max(EDGE, bx), max(EDGE, by)
    return (bx, by, bw, bh), (bx + 13.0, by + 8.0, text_w + 1.0, text_h)


def union(a, b):
    x1, y1 = min(a[0], b[0]), min(a[1], b[1])
    x2, y2 = max(a[0] + a[2], b[0] + b[2]), max(a[1] + a[3], b[1] + b[3])
    return (x1, y1, x2 - x1, y2 - y1)


def pointer_area(x: float, y: float, bubble=None):
    """Everything the pointer covers (to redraw only that part of the screen)."""
    area = (x - 44.0, y - 44.0, 88.0, 88.0)
    if bubble is not None:
        area = union(area, (bubble[0] - 10, bubble[1] - 10, bubble[2] + 20, bubble[3] + 20))
    return area


def glide(start, end, elapsed: float, duration: float = MOVE_SECONDS):
    """Position along a smooth (ease-in-out) glide, and whether it has arrived."""
    p = 1.0 if duration <= 0 else min(1.0, max(0.0, elapsed / duration))
    e = p * p * (3.0 - 2.0 * p)
    return (start[0] + (end[0] - start[0]) * e, start[1] + (end[1] - start[1]) * e), p >= 1.0


def from_mouse(mouse_x: float, mouse_y: float, screen_height: float) -> tuple[float, float]:
    """macOS reports the mouse from the bottom-left; the pointer works from the top-left."""
    return float(mouse_x), float(screen_height - mouse_y)
