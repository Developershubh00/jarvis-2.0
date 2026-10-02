"""Mac control tools: open things, run AppleScript, use the clipboard, type into apps, notifications."""
from __future__ import annotations

import os
import re
import subprocess
import time

from .. import mac
from . import Cancelled, ToolContext, ToolError, require, short
from .file_tools import resolve_path

# AppleScript containing any of these words is shown to the user first (when confirm_shell is "risky").
RISKY_APPLESCRIPT = re.compile(
    r"\b(?:delete|erase|empty\s+(?:the\s+)?trash|do\s+shell\s+script|send|shut\s*down|restart|"
    r"log\s*out|eject|remove)\b",
    re.IGNORECASE,
)
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:(?!\d)")          # https:, mailto:, vscode:, not localhost:3000
_LOCAL = re.compile(r"^(?:localhost|127\.0\.0\.1|0\.0\.0\.0)(?::\d+)?(?:/\S*)?$", re.IGNORECASE)
_DOMAIN = re.compile(r"^(?:www\.)?[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}(?::\d+)?(?:/\S*)?$")

_FILE_EXTS = {"md", "txt", "py", "js", "jsx", "ts", "tsx", "json", "html", "htm", "css", "pdf", "doc", "docx",
              "rtf", "csv", "xls", "xlsx", "ppt", "pptx", "png", "jpg", "jpeg", "gif", "svg", "zip", "sh", "yaml",
              "yml", "toml", "java", "c", "cpp", "h", "rb", "go", "rs", "swift", "kt", "log", "key", "pages",
              "numbers", "mp3", "mp4", "mov", "wav", "ipynb", "xml", "sql"}


def as_url(ctx: ToolContext, target: str) -> str | None:
    """The URL to open for ``target``, or None when it should be treated as a file or folder."""
    if _SCHEME.match(target):
        return target
    if _LOCAL.match(target):
        return "http://" + target
    if _DOMAIN.match(target) and not resolve_path(ctx, target).exists():
        host = target.split("/", 1)[0].split(":", 1)[0]
        if host.rsplit(".", 1)[-1].lower() not in _FILE_EXTS:  # notes.md is a file, not Moldova
            return "https://" + target
    return None


def open_target(ctx: ToolContext, a: dict) -> str:
    target = str(a.get("target") or "").strip()
    app = str(a.get("app") or "").strip()
    if not target and not app:
        raise ToolError("Give a target (URL, file or folder), an app, or both.")
    argv = ["open"]
    if a.get("background"):
        argv.append("-g")
    if app:
        argv += ["-a", app]
    if target:
        url = as_url(ctx, target)
        if url:
            argv.append(url)
        else:
            path = resolve_path(ctx, target)
            if not path.exists():
                raise ToolError(f"Nothing exists at {path}. For a website, give the address, like https://example.com.")
            argv.append(str(path))
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    except FileNotFoundError as e:
        raise ToolError("'open' is only available on macOS.") from e
    if proc.returncode != 0:
        raise ToolError((proc.stderr or proc.stdout).strip() or "open failed")
    return f"Opened {target or app}" + (f" in {app}" if app and target else "") + "."


def run_applescript(ctx: ToolContext, a: dict) -> str:
    script = str(require(a, "script"))
    mode = str(getattr(ctx.cfg.safety, "confirm_shell", "risky")).lower()
    if mode == "always" or (mode == "risky" and RISKY_APPLESCRIPT.search(script)):
        if not ctx.confirm("Jarvis wants to run an AppleScript", script):
            raise ToolError("The user declined this AppleScript. Don't retry it; ask what they'd prefer.")
    try:
        out = mac.osascript(script, timeout=float(a.get("timeout") or 60), cancel=ctx.cancel)
    except mac.AppleScriptError as e:
        if ctx.cancel.is_set():
            raise Cancelled() from e
        msg = str(e)
        if "-1743" in msg or "Not authorized" in msg:
            msg += ("\nHint: the user must allow automation for their terminal app in System Settings > "
                    "Privacy & Security > Automation.")
        elif "-1719" in msg or "-25211" in msg or "assistive" in msg:
            msg += "\nHint: this needs Accessibility permission (System Settings > Privacy & Security > Accessibility)."
        raise ToolError(msg) from e
    return out or "(done, no output)"


def clipboard(ctx: ToolContext, a: dict) -> str:
    action = str(a.get("action") or "get").lower()
    if action == "get":
        text = mac.get_clipboard()
        return text if text else "(the clipboard is empty or doesn't contain text)"
    if action == "set":
        text = a.get("text")
        if text is None:
            raise ToolError("'text' is required to set the clipboard.")
        try:
            mac.set_clipboard(str(text))
        except RuntimeError as e:
            raise ToolError(str(e)) from e
        return f"Copied {len(str(text))} characters to the clipboard."
    raise ToolError("action must be 'get' or 'set'.")


def _need_accessibility() -> None:
    if not mac.accessibility_trusted():
        raise ToolError("I need Accessibility permission to type or copy in other apps. The user should enable their "
                        "terminal app in System Settings > Privacy & Security > Accessibility and restart Jarvis. "
                        "Meanwhile, put the text on the clipboard instead.")


def _focus_original_app(ctx: ToolContext) -> str:
    target = ctx.frontmost or {}
    name = target.get("name") or "the frontmost app"
    if target.get("pid") and target.get("pid") != os.getpid():
        if not mac.activate_app(target):
            raise ToolError(f"I couldn't bring {name} back to the front. Ask the user to click into it and try again.")
        time.sleep(0.2)
    return name


def _not_into_own_terminal(ctx: ToolContext, what: str) -> None:
    host = ctx.host_app or {}
    if host.get("pid") and mac.frontmost_app().get("pid") == host["pid"]:
        raise ToolError(f"The terminal running Jarvis is in front, so I won't {what} there. Bring the right app "
                        "forward first (for example with the open tool), then try again.")


def type_text(ctx: ToolContext, a: dict) -> str:
    text = a.get("text")
    if not text:
        raise ToolError("'text' is required.")
    _need_accessibility()
    name = _focus_original_app(ctx)
    _not_into_own_terminal(ctx, "type")
    press_enter = bool(a.get("press_enter"))
    mac.paste_text(str(text), press_enter=press_enter)
    return f"Typed {len(str(text))} characters into {name}" + (" and pressed Return." if press_enter else ".")


def get_selected_text(ctx: ToolContext, a: dict) -> str:
    if (ctx.host_app or {}).get("pid") and not ctx.frontmost:
        raise ToolError("In terminal mode I can't reach a selection in another app. Ask the user to copy it "
                        "(Cmd+C), then read it with the clipboard tool.")
    _need_accessibility()
    name = _focus_original_app(ctx)
    text = mac.copy_selection()
    if not text:
        return (f"Nothing is selected in {name} (or the app doesn't allow copying). Ask the user to select the "
                "text, or use take_screenshot to read the screen.")
    return text


def frontmost(ctx: ToolContext, a: dict) -> str:
    current = mac.frontmost_app()
    lines = []
    if ctx.frontmost:
        lines.append(f"When the user called you: {ctx.frontmost.get('name')}"
                     + (f", window \"{ctx.frontmost.get('window')}\"" if ctx.frontmost.get("window") else ""))
    lines.append(f"Right now: {current.get('name') or 'unknown'}"
                 + (f", window \"{current.get('window')}\"" if current.get("window") else ""))
    return "\n".join(lines)


def notify(ctx: ToolContext, a: dict) -> str:
    mac.notify(str(a.get("title") or ctx.cfg.assistant_name), str(require(a, "message")))
    return "Notification shown."


def speak(ctx: ToolContext, a: dict) -> str:
    text = str(require(a, "text"))
    if ctx.speak is None:
        return "Speech is off right now, so the text is only on screen."
    ctx.speak(text)
    return "Said it."


def register(reg) -> None:
    reg.add(
        "open",
        """Open a URL in the default browser, a file in its default app, a folder in Finder, or launch an app.
        Combine target and app to open something in a specific app (e.g. a folder in "Visual Studio Code").""",
        {
            "target": {"type": "string", "description": "URL, file or folder path."},
            "app": {"type": "string", "description": "App name, e.g. \"Safari\", \"Visual Studio Code\", \"TextEdit\"."},
            "background": {"type": "boolean", "description": "Open without bringing it to the front."},
        },
        func=open_target,
        status=lambda a: "Opening " + short(a.get("target") or a.get("app") or "", 70),
        activity="Opening…",
    )
    reg.add(
        "run_applescript",
        """Run AppleScript to control macOS and apps: volume, dark mode, Music, Safari/Chrome tabs, Finder,
        Notes, Reminders, Calendar, Mail drafts, System Events UI scripting. Returns the script's result.
        Destructive or sending actions are shown to the user for approval.""",
        {
            "script": {"type": "string", "description": "AppleScript source code."},
            "timeout": {"type": "number", "description": "Seconds (default 60)."},
        },
        required=("script",), func=run_applescript,
        status=lambda a: "Running AppleScript: " + short(a.get("script", ""), 60),
        activity="Talking to an app…",
    )
    reg.add(
        "clipboard",
        "Read the clipboard text (action=get) or put text on it (action=set).",
        {
            "action": {"type": "string", "enum": ["get", "set"]},
            "text": {"type": "string", "description": "Text to copy (for action=set)."},
        },
        required=("action",), func=clipboard,
        status=lambda a: "Copying to the clipboard" if a.get("action") == "set" else "Reading the clipboard",
        activity="Using the clipboard…",
    )
    reg.add(
        "type_text",
        """Type text into the app the user was using when they called you (where their cursor is), e.g. to
        fill in a reply, a form field, a document or a code editor. Uses paste, so it's instant and keeps
        formatting simple. press_enter=true presses Return afterwards: only do that when the user clearly
        asked to send or submit.""",
        {
            "text": {"type": "string", "description": "Text to insert."},
            "press_enter": {"type": "boolean", "description": "Press Return after typing. Default false."},
        },
        required=("text",), func=type_text,
        status=lambda a: f"Typing {len(str(a.get('text', '')))} characters",
        activity="Getting ready to type…",
    )
    reg.add(
        "get_selected_text",
        """Get the text the user has selected in the app they were using (copies it, then restores their
        clipboard). Use when they say "this", "the selected text", "fix this", "explain this".""",
        {}, func=get_selected_text, status="Reading your selection", activity="Reading your selection…",
    )
    reg.add(
        "frontmost_app",
        "Which app and window the user was in when they called you, and which is in front right now.",
        {}, func=frontmost, status="Checking the front app", activity="Checking the front app…",
    )
    reg.add(
        "notify",
        "Show a macOS notification (useful for reminders of finished long tasks).",
        {
            "message": {"type": "string"},
            "title": {"type": "string"},
        },
        required=("message",), func=notify, status="Showing a notification", activity="Notifying…",
    )
    reg.add(
        "speak",
        """Say something out loud right now, before your final answer: a short progress update during a long
        task, or one step of a spoken walkthrough. Your final reply is spoken automatically; don't repeat it.""",
        {"text": {"type": "string", "description": "One or two short sentences."}},
        required=("text",), func=speak,
        status=lambda a: "Saying: " + short(a.get("text", ""), 70), activity="Speaking…",
    )
