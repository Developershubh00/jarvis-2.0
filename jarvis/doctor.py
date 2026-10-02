"""./run.sh --doctor: checks your setup and explains how to fix anything that's wrong."""
from __future__ import annotations

import importlib
import importlib.metadata
import platform
import sys
import textwrap
import time
from pathlib import Path

from . import PHASE, __version__
from .claude_client import explain_api_error, get_api_key, make_client, mask_key
from .config import ENV_PATH

OK, WARN, FAIL, INFO = "ok", "warn", "fail", "info"
_SYMBOL = {OK: "✓", WARN: "!", FAIL: "✗", INFO: "·"}
_COLOR = {OK: "32", WARN: "33", FAIL: "31", INFO: "2"}

# (import name, pip package, macOS only)
PACKAGES = [
    ("anthropic", "anthropic", False),
    ("yaml", "PyYAML", False),
    ("dotenv", "python-dotenv", False),
    ("numpy", "numpy", False),
    ("PIL", "Pillow", False),
    ("pypdf", "pypdf", False),
    ("sounddevice", "sounddevice", False),
    ("faster_whisper", "faster-whisper", False),
    ("objc", "pyobjc-core", True),
    ("AppKit", "pyobjc-framework-Cocoa", True),
    ("Quartz", "pyobjc-framework-Quartz", True),
    ("HIServices", "pyobjc-framework-ApplicationServices", True),
]


class Report:
    """Prints check results as they happen and counts them."""

    def __init__(self, out=None, color: bool | None = None):
        self.out = out or sys.stdout
        isatty = getattr(self.out, "isatty", lambda: False)
        self.color = bool(isatty()) if color is None else color
        self.items: list[tuple[str, str, str]] = []

    def paint(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.color else text

    def line(self, text: str = "") -> None:
        print(text, file=self.out, flush=True)

    def section(self, title: str) -> None:
        self.line()
        self.line(self.paint("1", title))

    def add(self, status: str, label: str, detail: str = "") -> None:
        self.items.append((status, label, detail))
        self.line(f"  {self.paint(_COLOR[status], _SYMBOL[status])} {label}")
        for part in textwrap.wrap(detail, 84):
            self.line(f"      {part}")

    def count(self, status: str) -> int:
        return sum(1 for s, _, _ in self.items if s == status)


def check_system(r: Report) -> None:
    r.section("System")
    if sys.platform == "darwin":
        version = platform.mac_ver()[0] or "?"
        try:
            major = int(version.split(".")[0])
        except ValueError:
            major = 0
        if major >= 12:
            r.add(OK, f"macOS {version}")
        else:
            r.add(WARN, f"macOS {version}", "macOS 12 (Monterey) or newer is recommended.")
    else:
        r.add(WARN, f"{platform.system()} {platform.release()}",
              "Jarvis is built for macOS. These checks still run, but the Mac features won't work here.")
    arch = platform.machine()
    r.add(INFO, f"Processor: {({'x86_64': 'Intel', 'arm64': 'Apple Silicon'}).get(arch, arch)} ({arch})")
    v = sys.version_info
    version = f"{v.major}.{v.minor}.{v.micro}"
    if (v.major, v.minor) in ((3, 11), (3, 12)):
        r.add(OK, f"Python {version}")
    elif (v.major, v.minor) >= (3, 13):
        r.add(WARN, f"Python {version}",
              "Use Python 3.12: speech recognition has no Intel-Mac build for 3.13+ yet. "
              "Install python@3.12, delete the .venv folder and run ./setup.sh again.")
    else:
        r.add(FAIL, f"Python {version}", "Python 3.11 or 3.12 is required. Install python@3.12 and run ./setup.sh again.")
    if sys.prefix != sys.base_prefix:
        r.add(OK, "Running inside the project's virtual environment")
    else:
        r.add(WARN, "Not running inside .venv", "Start Jarvis with ./run.sh so it uses the packages ./setup.sh installed.")


def check_packages(r: Report) -> None:
    r.section("Python packages")
    for module, dist, mac_only in PACKAGES:
        if mac_only and sys.platform != "darwin":
            r.add(INFO, f"{dist}: skipped (macOS only)")
            continue
        try:
            importlib.import_module(module)
        except Exception as e:  # ImportError, or OSError when a native library is missing
            hint = "Run ./setup.sh again."
            if "portaudio" in str(e).lower():
                hint = "The PortAudio sound library is missing (on Linux: sudo apt install libportaudio2)."
            r.add(FAIL, f"{dist} isn't working", f"{type(e).__name__}: {e}. {hint}")
            continue
        try:
            version = importlib.metadata.version(dist)
        except importlib.metadata.PackageNotFoundError:
            version = ""
        r.add(OK, f"{dist} {version}".strip())


def check_config(r: Report, cfg) -> None:
    r.section("Settings")
    files = [p.name for p in getattr(cfg, "loaded_files", [])]
    if files:
        r.add(OK, "Loaded " + " + ".join(files))
    else:
        r.add(WARN, "Using built-in defaults only", "config.yaml is missing; restore it from the repository.")
    for warning in cfg.warnings:
        r.add(WARN, warning)
    r.add(INFO, f"Model: {cfg.llm.model}")
    workspace = Path(cfg.paths.workspace)
    try:
        workspace.mkdir(parents=True, exist_ok=True)
        probe = workspace / ".jarvis-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        r.add(OK, f"Workspace: {workspace}")
    except OSError as e:
        r.add(FAIL, f"Workspace isn't writable: {workspace}", f"{e}. Set paths.workspace in config.local.yaml.")


def check_mac_permissions(r: Report) -> None:
    if sys.platform != "darwin":
        return
    from . import mac

    r.section("Mac permissions")
    if mac.accessibility_trusted(prompt=False):
        r.add(OK, "Accessibility: allowed (typing into apps, reading selected text)")
    else:
        r.add(WARN, "Accessibility: not allowed yet",
              "Needed to type into other apps and read selected text. Open System Settings, Privacy & Security, "
              "Accessibility, turn on your terminal app (Terminal or iTerm), then restart Jarvis.")
    r.add(INFO, "Automation: macOS asks the first time Jarvis controls each app (Music, Notes...). Click OK.")


def check_voice(r: Report, cfg, hardware: bool = True) -> None:
    r.section("Microphone and speech")
    from .voice.recorder import MIC_PERMISSION_HINT
    from .voice.stt import Transcriber

    stt = Transcriber(cfg)
    if stt.is_cached():
        r.add(OK, f"Speech model {stt.model_name}: downloaded")
    else:
        r.add(INFO, f"Speech model {stt.model_name}: not downloaded yet",
              f"It downloads by itself the first time you talk ({stt.download_size}, once). Try ./run.sh --listen.")
    if not hardware:
        return
    try:
        import numpy as np
        import sounddevice as sd
    except Exception as e:
        r.add(FAIL, "Microphone: sounddevice isn't working", f"{type(e).__name__}: {e}")
        return
    try:
        device = sd.query_devices(cfg.voice.input_device, kind="input")
        r.add(OK, f"Microphone: {device['name']}")
    except Exception as e:
        r.add(FAIL, "No microphone found", f"{e}. Check System Settings, Sound, Input, or set voice.input_device.")
        return
    try:
        audio = sd.rec(16_000, samplerate=16_000, channels=1, dtype="float32", device=cfg.voice.input_device)
        sd.wait()
    except Exception as e:
        r.add(FAIL, "Couldn't record from the microphone", str(e))
        return
    rms = float(np.sqrt(np.mean(np.square(audio))))
    if rms == 0.0:
        r.add(FAIL, "The microphone gives pure silence", MIC_PERMISSION_HINT)
    else:
        r.add(OK, f"Microphone level: {rms:.4f} during a 1-second test",
              f"Quiet rooms are usually below {cfg.voice.vad_threshold}; speech is well above it.")


def check_api(r: Report, cfg, client=None) -> None:
    r.section("Claude API")
    if client is None:
        key = get_api_key()
        if not key:
            r.add(FAIL, "No API key",
                  f"Run: open -e {ENV_PATH}  then paste your key after ANTHROPIC_API_KEY= and save. "
                  "Create a key at https://platform.claude.com.")
            return
        if key.startswith("sk-ant-"):
            r.add(OK, f"API key found: {mask_key(key)}")
        else:
            r.add(WARN, f"API key found: {mask_key(key)}",
                  "Anthropic keys start with sk-ant-. Check that you pasted the whole key.")
        try:
            client = make_client(timeout=30.0, max_retries=1)
        except Exception as e:
            r.add(FAIL, "Couldn't create the API client", explain_api_error(e, cfg.llm.model))
            return
    started = time.monotonic()
    try:
        reply = client.messages.create(
            model=cfg.llm.model,
            max_tokens=20,
            messages=[{"role": "user", "content": "Reply with exactly these words and nothing else: Jarvis online."}],
        )
    except Exception as e:
        r.add(FAIL, "Test message failed", explain_api_error(e, cfg.llm.model))
        return
    text = "".join(getattr(b, "text", "") for b in reply.content if getattr(b, "type", "") == "text").strip()
    r.add(OK, f"Claude replied: {text or '(empty reply)'}", f"{cfg.llm.model}, {time.monotonic() - started:.1f}s")


def run_doctor(cfg, out=None, client=None, hardware: bool = True) -> int:
    """Run every check. Returns 0 when nothing failed, 1 otherwise.

    hardware=False skips the 1-second microphone recording (the tests use that)."""
    r = Report(out)
    r.line(r.paint("1", f"Jarvis {__version__} doctor (phase {PHASE} of 10)"))
    check_system(r)
    check_packages(r)
    check_config(r, cfg)
    check_mac_permissions(r)
    check_voice(r, cfg, hardware)
    check_api(r, cfg, client)
    fails, warns = r.count(FAIL), r.count(WARN)
    r.line()
    if fails:
        r.line(f"{fails} problem(s) to fix (marked ✗ above). Fix them, then run ./run.sh --doctor again.")
    elif warns:
        r.line(f"Working, with {warns} warning(s) worth a look (marked ! above).")
    else:
        r.line("Everything looks good.")
    return 1 if fails else 0
