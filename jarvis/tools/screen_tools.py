"""Screen tools: look at the screen and point at things with the tutor cursor."""
from __future__ import annotations

import sys

from .. import mac
from . import Cancelled, ToolContext, ToolError, ToolResult, short

GLIDE_SECONDS = 0.6  # let the pointer arrive before explaining


def reading_time(label: str) -> float:
    """How long a label stays up when it isn't spoken."""
    return min(4.0, 0.8 + 0.05 * len(label))


def _pause(ctx: ToolContext, seconds: float) -> None:
    if seconds > 0 and ctx.cancel.wait(seconds):
        raise Cancelled()


def take_screenshot(ctx: ToolContext, a: dict) -> ToolResult:
    if sys.platform != "darwin":
        raise ToolError("Screenshots need macOS.")
    allowed = mac.screen_recording_allowed()
    try:
        if ctx.ui is not None:
            with ctx.ui.hidden_for_capture():
                shot = mac.capture_screen()
        else:
            shot = mac.capture_screen()
    except Exception as e:
        raise ToolError(f"Couldn't take a screenshot: {e}") from e
    ctx.shot = shot
    text = (f"Screenshot of the user's main display, {shot.width}x{shot.height} px. "
            "point_at uses pixel coordinates in this image.")
    if allowed is False:
        mac.screen_recording_allowed(request=True)
        text += ("\nWarning: Screen Recording permission is off, so other apps' windows may be missing. Tell the "
                 "user to enable their terminal app in System Settings > Privacy & Security > Screen Recording, "
                 "then quit and reopen the terminal.")
    return ToolResult(text=text, image_jpeg=shot.jpeg)


def point_at(ctx: ToolContext, a: dict) -> str:
    shot = ctx.shot
    if shot is None:
        raise ToolError("Take a screenshot first (take_screenshot), then point using its pixel coordinates.")
    try:
        x = float(a.get("x"))
        y = float(a.get("y"))
    except (TypeError, ValueError) as e:
        raise ToolError("x and y must be numbers: pixel coordinates in the latest screenshot.") from e
    x = min(max(x, 0.0), shot.width - 1.0)
    y = min(max(y, 0.0), shot.height - 1.0)
    label = " ".join(str(a.get("label") or "").split())[:160]
    sx, sy = shot.to_screen(x, y)
    if ctx.ui is not None:
        ctx.ui.point_at(sx, sy, label)
    _pause(ctx, GLIDE_SECONDS)
    if label and ctx.speak is not None and a.get("say_label", True):
        ctx.speak(label)
    elif label:
        _pause(ctx, reading_time(label))  # speech is off: give the user time to read it
    ctx.check_cancel()
    return f"Pointing at ({int(x)}, {int(y)})" + (f": {label}" if label else "") + "."


def hide_pointer(ctx: ToolContext, a: dict) -> str:
    if ctx.ui is not None:
        ctx.ui.hide_pointer()
    return "Pointer hidden."


def register(reg) -> None:
    reg.add(
        "take_screenshot",
        """See the user's main screen (the menu bar screen). Returns an image; coordinates for point_at are
        pixels in this image. Take a fresh one whenever the screen may have changed.""",
        {}, func=take_screenshot, status="Looking at your screen", activity="Looking at your screen…",
    )
    reg.add(
        "point_at",
        """Show the user where something is: a glowing tutor cursor glides to (x, y) on their screen, with
        your label in a bubble next to it, and the label is spoken aloud. Coordinates are pixels in the most
        recent screenshot (origin top-left); aim for the centre of the element. Use one call per step to walk
        the user through a sequence; each call waits until the label has been spoken.""",
        {
            "x": {"type": "number", "description": "X pixel in the latest screenshot."},
            "y": {"type": "number", "description": "Y pixel in the latest screenshot."},
            "label": {"type": "string", "description": "Short instruction, a few words (e.g. \"Click Run here\")."},
            "say_label": {"type": "boolean", "description": "Speak the label. Default true."},
        },
        required=("x", "y", "label"), func=point_at,
        status=lambda a: "Pointing: " + short(a.get("label", ""), 70), activity="Finding the spot…",
    )
    reg.add(
        "hide_pointer",
        "Hide the tutor cursor.",
        {}, func=hide_pointer, status="Hiding the pointer", activity="Tidying up…",
    )
