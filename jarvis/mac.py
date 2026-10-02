"""Thread-safe macOS helpers: AppleScript, dialogs, clipboard, synthetic keys, screenshots, permissions.

Everything here degrades gracefully (returns a neutral value or raises a clear error) when the
macOS frameworks are unavailable, so the rest of the app stays testable on other systems.
"""
from __future__ import annotations

import base64
import io
import logging
import os
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

KEY_C = 8
KEY_V = 9
KEY_RETURN = 36
FLAG_CMD = 0x100000

SETTINGS_URLS = {
    "accessibility": "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
    "screen": "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture",
    "microphone": "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone",
    "input": "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent",
}


class AppleScriptError(RuntimeError):
    pass


# ---------------------------------------------------------------- AppleScript

def as_quote(text: str) -> str:
    """Return text as a safely escaped AppleScript string literal."""
    text = str(text)
    text = text.replace("\\", "\\\\").replace('"', '\\"')
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n").replace("\t", "\\t")
    return f'"{text}"'


def osascript(script: str, timeout: float = 60, cancel: threading.Event | None = None) -> str:
    """Run AppleScript source via osascript (script passed on stdin). Raises AppleScriptError."""
    try:
        proc = subprocess.Popen(
            ["osascript"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
    except FileNotFoundError as e:
        raise AppleScriptError("osascript is not available (this feature needs macOS).") from e
    try:
        if cancel is None:
            out, err = proc.communicate(script, timeout=timeout)
        else:
            proc.stdin.write(script)
            proc.stdin.close()
            deadline = time.monotonic() + timeout
            while proc.poll() is None:
                if cancel.is_set():
                    proc.kill()
                    proc.wait()
                    raise AppleScriptError("cancelled")
                if time.monotonic() > deadline:
                    raise subprocess.TimeoutExpired(proc.args, timeout)
                time.sleep(0.1)
            out, err = proc.stdout.read(), proc.stderr.read()
    except subprocess.TimeoutExpired as e:
        proc.kill()
        proc.wait()
        raise AppleScriptError(f"AppleScript timed out after {int(timeout)}s") from e
    if proc.returncode != 0:
        raise AppleScriptError((err or "").strip() or f"osascript exited with {proc.returncode}")
    return (out or "").rstrip("\n")


def notify(title: str, message: str, subtitle: str = "") -> None:
    script = f"display notification {as_quote(message[:240])} with title {as_quote(title)}"
    if subtitle:
        script += f" subtitle {as_quote(subtitle)}"
    try:
        osascript(script, timeout=10)
    except AppleScriptError as e:
        log.warning("notify failed: %s", e)


def confirm_dialog(title: str, message: str, ok: str = "Allow", cancel_label: str = "Don't allow",
                   timeout: int = 120, cancel: threading.Event | None = None) -> bool:
    """Show a focused confirmation dialog. Safe default: Return/Esc/timeout all mean "no"."""
    script = f"""
try
    activate
end try
try
    set r to display dialog {as_quote(message[:1500])} with title {as_quote(title)} buttons {{{as_quote(cancel_label)}, {as_quote(ok)}}} default button {as_quote(cancel_label)} cancel button {as_quote(cancel_label)} with icon caution giving up after {int(timeout)}
    if gave up of r then return "timeout"
    return button returned of r
on error number -128
    return "no"
end try
"""
    try:
        answer = osascript(script, timeout=timeout + 15, cancel=cancel)
    except AppleScriptError as e:
        log.warning("confirm dialog failed: %s", e)
        return False
    return answer == ok


def ask_text_dialog(prompt: str, title: str = "Jarvis", default: str = "",
                    cancel: threading.Event | None = None) -> str | None:
    """Ask the user to type something. Returns None if cancelled or empty."""
    script = f"""
try
    activate
end try
try
    set r to display dialog {as_quote(prompt)} with title {as_quote(title)} default answer {as_quote(default)} buttons {{"Cancel", "Go"}} default button "Go" cancel button "Cancel" giving up after 600
    if gave up of r then return ""
    return text returned of r
on error number -128
    return ""
end try
"""
    try:
        text = osascript(script, timeout=620, cancel=cancel)
    except AppleScriptError as e:
        log.warning("text dialog failed: %s", e)
        return None
    text = text.strip()
    return text or None


# ---------------------------------------------------------------- clipboard

def _utf8_env() -> dict:
    env = dict(os.environ)
    if "UTF-8" not in env.get("LANG", "").upper():
        env["LANG"] = "en_US.UTF-8"
    return env


def get_clipboard() -> str:
    try:
        p = subprocess.run(["pbpaste"], capture_output=True, env=_utf8_env(), timeout=5)
        return p.stdout.decode("utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError):
        return ""


def set_clipboard(text: str) -> None:
    try:
        subprocess.run(["pbcopy"], input=text.encode("utf-8"), env=_utf8_env(), timeout=5, check=True)
    except (OSError, subprocess.SubprocessError) as e:
        raise RuntimeError(f"Couldn't set the clipboard: {e}") from e


def clipboard_change_count() -> int:
    try:
        from AppKit import NSPasteboard

        return int(NSPasteboard.generalPasteboard().changeCount())
    except Exception:
        return -1


# ---------------------------------------------------------------- synthetic keys

def post_key(keycode: int, flags: int = 0) -> None:
    import Quartz

    source = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
    down = Quartz.CGEventCreateKeyboardEvent(source, keycode, True)
    up = Quartz.CGEventCreateKeyboardEvent(source, keycode, False)
    Quartz.CGEventSetFlags(down, flags)
    Quartz.CGEventSetFlags(up, flags)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, down)
    time.sleep(0.015)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)


def paste_text(text: str, press_enter: bool = False) -> None:
    """Type text into the frontmost app by pasting it, then restore the previous clipboard."""
    previous = get_clipboard()
    set_clipboard(text)
    ours = clipboard_change_count()
    time.sleep(0.05)
    post_key(KEY_V, FLAG_CMD)
    if press_enter:
        time.sleep(0.15)
        post_key(KEY_RETURN, 0)
    if previous:
        def restore() -> None:
            time.sleep(0.8)
            # Only restore if nothing else touched the clipboard in the meantime.
            if clipboard_change_count() in (ours, -1):
                try:
                    set_clipboard(previous)
                except RuntimeError:
                    pass

        threading.Thread(target=restore, daemon=True).start()


def copy_selection(wait: float = 0.8) -> str:
    """Copy the current selection in the frontmost app and return it (clipboard is restored)."""
    previous = get_clipboard()
    before = clipboard_change_count()
    post_key(KEY_C, FLAG_CMD)
    deadline = time.monotonic() + wait
    changed = False
    while time.monotonic() < deadline:
        time.sleep(0.05)
        if clipboard_change_count() != before:
            changed = True
            break
    if not changed:
        return ""
    time.sleep(0.05)
    text = get_clipboard()
    try:
        set_clipboard(previous)
    except RuntimeError:
        pass
    return text


# ---------------------------------------------------------------- apps & windows

def frontmost_app() -> dict:
    """Return {'name', 'pid', 'bundle_id', 'window'} for the app the user is looking at."""
    info = {"name": "", "pid": 0, "bundle_id": "", "window": ""}
    my_pid = os.getpid()
    try:
        from AppKit import NSWorkspace

        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        if app is not None and int(app.processIdentifier()) != my_pid:
            info["name"] = str(app.localizedName() or "")
            info["pid"] = int(app.processIdentifier())
            info["bundle_id"] = str(app.bundleIdentifier() or "")
    except Exception:
        pass
    try:
        import Quartz

        options = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
        windows = Quartz.CGWindowListCopyWindowInfo(options, Quartz.kCGNullWindowID) or []
        for w in windows:
            if int(w.get("kCGWindowLayer", 1)) != 0:
                continue
            pid = int(w.get("kCGWindowOwnerPID", 0))
            if pid == my_pid:
                continue
            if info["pid"] and pid != info["pid"]:
                continue
            if not info["pid"]:
                info["pid"] = pid
                info["name"] = str(w.get("kCGWindowOwnerName", "") or "")
            info["window"] = str(w.get("kCGWindowName", "") or "")
            break
    except Exception:
        pass
    return info


def activate_app(info: dict | None) -> bool:
    """Bring a previously captured frontmost app back to the front."""
    if not info or not info.get("pid"):
        return False
    if frontmost_app().get("pid") == info["pid"]:
        return True
    try:
        from AppKit import NSRunningApplication

        app = NSRunningApplication.runningApplicationWithProcessIdentifier_(info["pid"])
        if app is not None:
            app.activateWithOptions_(2)  # NSApplicationActivateIgnoringOtherApps
            for _ in range(10):
                time.sleep(0.05)
                if frontmost_app().get("pid") == info["pid"]:
                    return True
    except Exception:
        pass
    try:  # LaunchServices fallback works even when cooperative activation refuses.
        if info.get("bundle_id"):
            subprocess.run(["open", "-b", info["bundle_id"]], timeout=10, capture_output=True)
        elif info.get("name"):
            subprocess.run(["open", "-a", info["name"]], timeout=10, capture_output=True)
        time.sleep(0.4)
        return frontmost_app().get("pid") == info["pid"]
    except Exception:
        return False


# ---------------------------------------------------------------- screen

@dataclass
class Shot:
    """A screenshot of the main display, downscaled for the model."""

    jpeg: bytes
    width: int          # image pixels (what the model sees)
    height: int
    screen_w: float     # main display size in points (what the UI uses)
    screen_h: float
    taken_at: float = field(default_factory=time.time)

    def to_screen(self, x: float, y: float) -> tuple[float, float]:
        x = min(max(float(x), 0.0), self.width - 1)
        y = min(max(float(y), 0.0), self.height - 1)
        return x * self.screen_w / self.width, y * self.screen_h / self.height

    def b64(self) -> str:
        return base64.b64encode(self.jpeg).decode("ascii")

    def image_block(self) -> dict:
        return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": self.b64()}}


def main_display_size() -> tuple[float, float]:
    try:
        import Quartz

        b = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID())
        return float(b.size.width), float(b.size.height)
    except Exception:
        return 1440.0, 900.0


def capture_screen(max_w: int = 1280, max_h: int = 800) -> Shot:
    from PIL import Image

    fd, path = tempfile.mkstemp(prefix="jarvis-shot-", suffix=".png")
    os.close(fd)
    try:
        p = subprocess.run(["screencapture", "-x", "-m", "-t", "png", path], capture_output=True, timeout=15)
        if p.returncode != 0 or os.path.getsize(path) == 0:
            raise RuntimeError("screencapture failed: " + p.stderr.decode(errors="replace").strip())
        with Image.open(path) as im:
            im = im.convert("RGB")
            im.thumbnail((max_w, max_h), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=80)
            w, h = im.size
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    sw, sh = main_display_size()
    return Shot(jpeg=buf.getvalue(), width=w, height=h, screen_w=sw, screen_h=sh)


# ---------------------------------------------------------------- sounds

def play_sound(name: str, volume: float = 0.5) -> None:
    path = f"/System/Library/Sounds/{name}.aiff"
    if not os.path.exists(path):
        return
    try:
        subprocess.Popen(["afplay", "-v", str(volume), path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


# ---------------------------------------------------------------- permissions

def accessibility_trusted(prompt: bool = False) -> bool:
    for module in ("HIServices", "ApplicationServices"):
        try:
            mod = __import__(module)
            fn = getattr(mod, "AXIsProcessTrustedWithOptions")
            key = getattr(mod, "kAXTrustedCheckOptionPrompt", "AXTrustedCheckOptionPrompt")
            return bool(fn({key: bool(prompt)}))
        except Exception:
            continue
    return False


def screen_recording_allowed(request: bool = False) -> bool | None:
    try:
        import Quartz

        if Quartz.CGPreflightScreenCaptureAccess():
            return True
        if request:
            Quartz.CGRequestScreenCaptureAccess()
        return False
    except Exception:
        return None


def input_monitoring_allowed() -> bool | None:
    try:
        import Quartz

        return bool(Quartz.CGPreflightListenEventAccess())
    except Exception:
        return None


def open_settings(pane: str) -> None:
    url = SETTINGS_URLS.get(pane)
    if url:
        subprocess.Popen(["open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
