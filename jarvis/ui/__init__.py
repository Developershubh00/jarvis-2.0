"""The UI bridge. The core talks to the screen only through these methods, so the same code runs in
the terminal (ConsoleUI) and, from phase 7, in the menu-bar app."""
from __future__ import annotations

import contextlib
import os
import sys
import threading
import time
from typing import Iterator


class UIBridge:
    """Every method is optional; the defaults do nothing."""

    def set_state(self, state: str, title: str | None = None, subtitle: str | None = None) -> None: ...
    def set_level(self, level: float) -> None: ...
    def set_activity(self, text: str) -> None: ...
    def begin_turn(self) -> None: ...
    def show_user_text(self, text: str) -> None: ...
    def append_text(self, text: str) -> None: ...
    def show_status(self, text: str) -> None: ...
    def show_error(self, text: str) -> None: ...
    def show_response(self, text: str) -> None: ...
    def hint(self, text: str) -> None: ...
    def schedule_hide(self, seconds: float | None = None) -> None: ...
    def point_at(self, x: float, y: float, label: str) -> None: ...
    def hide_pointer(self) -> None: ...
    def hide_pointer_after(self, seconds: float) -> None: ...

    @contextlib.contextmanager
    def hidden_for_capture(self) -> Iterator[None]:
        yield

    def confirm(self, title: str, message: str) -> bool:
        return False

    def ask_text(self, prompt: str) -> str | None:
        return None


class ConsoleUI(UIBridge):
    """Terminal version, used by --cli, --say and the tests."""

    def __init__(self, stream=None, interactive: bool | None = None, confirm_answer: bool | None = None,
                 color: bool | None = None, name: str = "Jarvis") -> None:
        self.out = stream or sys.stdout
        self.interactive = sys.stdin.isatty() if interactive is None else interactive
        self.confirm_answer = confirm_answer
        isatty = getattr(self.out, "isatty", lambda: False)
        self.color = (bool(isatty()) and not os.environ.get("NO_COLOR")) if color is None else color
        self.prefix = f"{name.lower()} › "
        self._lock = threading.Lock()
        self._midline = False
        self._streamed = False
        self._need_prefix = True
        self._meter = False
        self._last_meter = 0.0
        self.pointer: tuple[float, float, str] | None = None

    def _paint(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.color else text

    def _write(self, text: str) -> None:
        with self._lock:
            self.out.write(text)
            self.out.flush()
            if text:
                self._midline = not text.endswith("\n")

    def _line(self, text: str) -> None:
        self._clear_meter()
        prefix = "\n" if self._midline else ""
        self._write(f"{prefix}{text}\n")

    def _clear_meter(self) -> None:
        if self._meter:
            self._meter = False
            self._write("\r" + " " * 40 + "\r")
            self._midline = False

    def set_level(self, level: float) -> None:
        """A live microphone meter on one line (terminal only)."""
        now = time.monotonic()
        if not self.color or (now - self._last_meter < 0.05 and level > 0):
            return
        self._last_meter = now
        bars = max(0, min(20, int(round(level * 20))))
        self._write("\r  " + self._paint("35", "▮" * bars) + self._paint("2", "▯" * (20 - bars)) + " ")
        self._meter = True

    def set_state(self, state: str, title: str | None = None, subtitle: str | None = None) -> None:
        if state == "listening":
            self._line(self._paint("1;35", "● Listening.") +
                       self._paint("2", " Speak now; it stops when you pause. Ctrl+C cancels."))
        elif state == "transcribing":
            self._line(self._paint("2", "  · transcribing…"))
        elif state == "error" and subtitle:
            self.show_error(subtitle)
        elif title and state == "idle" and subtitle:
            self._line(f"[{title}] {subtitle}")

    def begin_turn(self) -> None:
        self._streamed = False
        self._need_prefix = True

    def show_user_text(self, text: str) -> None:
        self._line(f'You said: "{text}"')

    def append_text(self, text: str) -> None:
        if not text:
            return
        if self._need_prefix:
            if self._midline:
                self._write("\n")
            self._write(self._paint("1;36", self.prefix))
            self._need_prefix = False
        self._streamed = True
        self._write(text)

    def show_status(self, text: str) -> None:
        self._line(self._paint("2", f"  · {text}"))
        self._need_prefix = True

    def show_error(self, text: str) -> None:
        self._line(self._paint("31", f"Error: {text}"))

    def show_response(self, text: str) -> None:
        if not self._streamed and text:
            self.append_text(text)
        if self._midline:
            self._write("\n")

    def hint(self, text: str) -> None:
        self._line(self._paint("2", f"  ({text})"))

    def point_at(self, x: float, y: float, label: str) -> None:
        self.pointer = (x, y, label)
        self._line(f"  (pointing at {x:.0f},{y:.0f}: {label})")

    def hide_pointer(self) -> None:
        self.pointer = None

    def confirm(self, title: str, message: str) -> bool:
        if self.confirm_answer is not None:
            return self.confirm_answer
        if not self.interactive:
            return False
        self._line(f"\n{title}:\n{message}")
        try:
            return input("Allow? [y/N] ").strip().lower() in ("y", "yes")
        except EOFError:
            return False

    def ask_text(self, prompt: str) -> str | None:
        if not self.interactive:
            return None
        try:
            return input(f"{prompt} ").strip() or None
        except EOFError:
            return None
