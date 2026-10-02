"""Tool framework: what Claude can call, how calls run, and what comes back."""
from __future__ import annotations

import base64
import importlib
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

MAX_TOOL_TEXT = 15_000


class ToolError(Exception):
    """An expected failure. The message goes back to Claude as an error result."""


class Cancelled(Exception):
    """The user stopped the current request (Esc, Ctrl+C, menu)."""


@dataclass
class ToolResult:
    text: str = ""
    image_jpeg: bytes | None = None
    is_error: bool = False

    def to_content(self) -> str | list[dict]:
        """Content for a tool_result block: a string, or image + text blocks."""
        if self.image_jpeg:
            blocks: list[dict] = [{
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": base64.b64encode(self.image_jpeg).decode("ascii"),
                },
            }]
            if self.text:
                blocks.append({"type": "text", "text": self.text})
            return blocks
        return self.text if self.text else "(no output)"


@dataclass
class ToolContext:
    """Everything a tool may need for one request."""

    cfg: Any
    ui: Any = None
    memory: Any = None
    cancel: threading.Event = field(default_factory=threading.Event)
    frontmost: dict | None = None       # the app the user was in when they called Jarvis
    shot: Any = None                    # latest mac.Shot (needed by point_at)
    speak: Callable[[str], None] | None = None  # blocking speech, raises Cancelled if stopped
    host_app: dict | None = None        # the terminal running Jarvis in --cli mode (never type into it)

    @property
    def workspace(self) -> Path:
        return Path(self.cfg.paths.workspace)

    def check_cancel(self) -> None:
        if self.cancel.is_set():
            raise Cancelled()

    def confirm(self, title: str, message: str) -> bool:
        if self.ui is None:
            return False
        try:
            return bool(self.ui.confirm(title, message))
        except Cancelled:
            raise
        except Exception:
            log.exception("confirm dialog failed")
            return False


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict
    func: Callable[[ToolContext, dict], Any]
    status: Callable[[dict], str] | str | None = None   # line shown when it runs
    activity: str = ""                                   # shown while Claude is still writing the call


def require(args: dict, key: str) -> Any:
    value = args.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ToolError(f"'{key}' is required.")
    return value


def truncate(text: str, limit: int = MAX_TOOL_TEXT) -> str:
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    head = int(limit * 0.6)
    tail = limit - head
    omitted = len(text) - head - tail
    return f"{text[:head]}\n\n[... {omitted} characters omitted ...]\n\n{text[-tail:]}"


def short(text: Any, n: int = 70) -> str:
    s = " ".join(str(text).split())
    return s if len(s) <= n else s[: n - 1] + "…"


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def add(self, name: str, description: str, properties: dict, required: tuple | list = (),
            func: Callable | None = None, status: Callable | str | None = None, activity: str = "") -> None:
        schema: dict = {"type": "object", "properties": properties}
        if required:
            schema["required"] = list(required)
        self._tools[name] = Tool(name, " ".join(description.split()), schema, func, status, activity)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def schemas(self) -> list[dict]:
        return [{"name": t.name, "description": t.description, "input_schema": t.input_schema}
                for t in self._tools.values()]

    def summarize(self, name: str, args: dict | None) -> str:
        tool = self._tools.get(name)
        if tool is None:
            return f"Using {name}"
        if callable(tool.status):
            try:
                return tool.status(args or {})
            except Exception:
                pass
        if isinstance(tool.status, str) and tool.status:
            return tool.status
        return "Using " + name.replace("_", " ")

    def activity(self, name: str) -> str:
        tool = self._tools.get(name)
        if tool is not None and tool.activity:
            return tool.activity
        return "Working…"

    def execute(self, name: str, args: Any, ctx: ToolContext) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None or tool.func is None:
            return ToolResult(f"Unknown tool: {name}", is_error=True)
        if not isinstance(args, dict):
            args = {}
        try:
            out = tool.func(ctx, args)
        except Cancelled:
            raise
        except ToolError as e:
            return ToolResult(str(e), is_error=True)
        except Exception as e:  # a bug or an unexpected OS error: report it, keep going
            log.exception("Tool %s failed", name)
            return ToolResult(f"{type(e).__name__}: {e}", is_error=True)
        if out is None:
            out = ToolResult("Done.")
        elif isinstance(out, str):
            out = ToolResult(out)
        out.text = truncate(out.text or "")
        return out


# Tool modules, in the order their tools are offered to Claude. Each later phase adds modules here.
TOOL_MODULES = ("file_tools", "shell_tools", "mac_tools", "memory_tools")


def build_registry(cfg: Any = None) -> ToolRegistry:
    registry = ToolRegistry()
    for name in TOOL_MODULES:
        importlib.import_module(f"{__name__}.{name}").register(registry)
    return registry
