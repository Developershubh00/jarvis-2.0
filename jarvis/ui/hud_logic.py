"""The floating panel's plain-Python side: what the orb looks like in each state, how the conversation
text is laid out, and where the panel sits. The AppKit side (hud.py) only draws what this describes,
so all of it is tested anywhere."""
from __future__ import annotations

import math

WIDTH, HEIGHT, PAD, ORB = 440.0, 250.0, 18.0, 52.0

STATE_COLORS = {
    "idle": "#6F8BA6", "listening": "#7FE3FF", "transcribing": "#9DB4FF", "thinking": "#9DB4FF",
    "speaking": "#FFB547", "waiting": "#FFB547", "error": "#FF7A6B",
}
TITLES = {
    "idle": "Ready", "listening": "Listening", "transcribing": "Catching that", "thinking": "Working on it",
    "speaking": "Speaking", "waiting": "Waiting for you", "error": "Something went wrong",
}


def title_for(state: str, title: str | None = None) -> str:
    return title or TITLES.get(state, state.capitalize())


def text_to_append(current: str, text: str, kind: str, last_kind: str) -> str:
    """Status and error lines sit on lines of their own; reply text after them starts a new line."""
    if kind in ("status", "error"):
        text = text.strip() + "\n"
        if current and not current.endswith("\n"):
            text = "\n" + text
    elif last_kind in ("status", "error") and current and not current.endswith("\n"):
        text = "\n" + text
    return text


def orb_shapes(state: str, t: float, level: float, size: float) -> list[tuple]:
    """The orb as simple shapes centred in a square view of side ``size``:
    ("disc", radius, alpha), ("ring", radius, alpha, width), ("arc", radius, start, end, alpha, width),
    and last ("core", radius) for the glowing ball. ``t`` is seconds, ``level`` the smoothed mic level."""
    outer = size / 2.0 - 1.0
    core = outer * 0.46
    shapes: list[tuple] = []
    if state == "listening":  # halo that swells with your voice
        for i, alpha in enumerate((0.14, 0.26)):
            shapes.append(("disc", core + (outer - core) * (0.35 + 0.65 * level) * (1.0 - 0.3 * i), alpha))
        core *= 1.0 + 0.14 * level
    elif state in ("thinking", "transcribing", "waiting"):  # three arcs circling at different speeds
        for i in range(3):
            start = (t * (150 + 70 * i) + i * 120) % 360
            shapes.append(("arc", outer - 2 - i * 4.5, start, start + 70 + 25 * i, 0.85 - i * 0.22, 2.2))
    elif state == "speaking":  # ripples flowing outwards
        for i in range(3):
            p = (t * 0.8 + i / 3.0) % 1.0
            shapes.append(("ring", core + (outer - core) * p, 0.5 * (1.0 - p), 2.0))
    else:  # idle or error: a slow breath
        breathe = 0.5 + 0.5 * math.sin(t * 1.6)
        shapes.append(("disc", core + 4 + 3 * breathe, 0.10 + 0.08 * breathe))
    shapes.append(("core", core))
    return shapes


def smooth_level(current: float, target: float, dt: float) -> float:
    """Ease the mic level so the halo glides instead of flickering."""
    return current + (target - current) * min(1.0, dt * 12.0)


def panel_origin(visible: tuple[float, float, float, float], position: str,
                 width: float = WIDTH, height: float = HEIGHT, margin: float = 14.0) -> tuple[float, float]:
    """Bottom-left corner of the panel inside the screen's visible frame (x, y, w, h; y grows upwards)."""
    x0, y0, w, h = visible
    position = (position or "top-right").lower()
    x = x0 + w - width - margin
    if "left" in position:
        x = x0 + margin
    elif "center" in position:
        x = x0 + (w - width) / 2.0
    y = y0 + margin if "bottom" in position else y0 + h - height - margin
    return x, y
