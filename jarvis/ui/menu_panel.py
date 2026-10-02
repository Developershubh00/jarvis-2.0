"""Phase 7's stand-in for the floating panel. Jarvis shows what it's doing through the menu bar (the
icon, and a status line at the top of its menu) and uses notifications for things you need to see.
The glass panel arrives in phase 8 with the same methods; the tutor pointer arrives in phase 9."""
from __future__ import annotations

import time
from typing import Callable

STATUS = {"idle": "Ready", "listening": "Listening", "transcribing": "Transcribing", "thinking": "Thinking",
          "speaking": "Speaking", "waiting": "Waiting for you", "error": "Something went wrong"}
REPEAT_GAP = 2.0  # seconds before the same notification may be shown again


def short(text: str, n: int = 70) -> str:
    s = " ".join(str(text).split())
    return s if len(s) <= n else s[: n - 1] + "…"


class MenuPanel:
    def __init__(self, set_status: Callable[[str], None], notify: Callable[[str, str], None],
                 name: str = "Jarvis", idle_hint: str = "", notify_replies: bool = False) -> None:
        self.set_status_line = set_status
        self.notify = notify
        self.name = name
        self.idle_hint = idle_hint
        self.notify_replies = notify_replies   # when spoken replies are off, show them as notifications
        self.state = "idle"
        self.status = ""
        self.reply = ""
        self._last_notice = ("", 0.0)

    def _status(self, text: str) -> None:
        self.status = text
        self.set_status_line(text)

    def notice(self, title: str, message: str) -> None:
        key, now = f"{title}|{message}", time.monotonic()
        if key == self._last_notice[0] and now - self._last_notice[1] < REPEAT_GAP:
            return
        self._last_notice = (key, now)
        self.notify(title, message)

    # ---------------------------------------------------------- the panel's methods
    def set_state(self, state: str, title: str | None = None, subtitle: str | None = None) -> None:
        self.state = state
        if state == "error":
            heading = title or STATUS["error"]
            self._status(f"{heading}: {subtitle}" if subtitle else heading)
            self.notice(heading, subtitle or heading)
        elif state == "idle" and not title and not subtitle:
            self._status(f"Ready. {self.idle_hint}".strip())
        else:
            label = title or STATUS.get(state, state.capitalize())
            self._status(f"{label}. {subtitle}" if subtitle else f"{label}…")

    def set_subtitle(self, text: str) -> None:
        if text:
            self._status(text)

    def append(self, text: str, kind: str = "response") -> None:
        if kind == "response":
            self.reply += text
        elif kind == "error":
            self._status(short(text, 200))
            self.notice(self.name, text)
        elif text:
            self._status(text)

    def clear_text(self) -> None:
        self.reply = ""

    def text_is_empty(self) -> bool:
        return not self.reply.strip()

    def reply_done(self, text: str) -> None:
        if self.notify_replies and text:
            self.notice(self.name, short(text, 220))

    def show(self) -> None:
        if self.status:
            self.notice(self.name, self.status)

    def schedule_hide(self, seconds: float | None = None) -> None: ...
    def set_level(self, level: float) -> None: ...
    def set_capture_hidden(self, hidden: bool) -> None: ...


class NullOverlay:
    """The tutor pointer arrives in phase 9."""

    def point_at(self, x: float, y: float, label: str) -> None: ...
    def hide(self) -> None: ...
    def hide_after(self, seconds: float) -> None: ...
    def set_capture_hidden(self, hidden: bool) -> None: ...
