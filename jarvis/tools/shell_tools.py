"""Shell tool: run terminal commands, with approval for risky ones and background jobs for servers."""
from __future__ import annotations

import collections
import os
import re
import signal
import subprocess
import threading
import time
from pathlib import Path

from . import Cancelled, ToolContext, ToolError, ToolResult, require, short
from .file_tools import resolve_path

# Commands matching any of these are shown to the user for approval (when confirm_shell is "risky").
_CMD = r"(?:^|[\s;&|(`]|\$\()"   # command position
RISKY_SHELL_PATTERNS = [
    _CMD + r"sudo\b",
    _CMD + r"su\s",
    _CMD + r"rm\s",
    _CMD + r"rmdir\s",
    _CMD + r"unlink\s",
    _CMD + r"(?:shred|srm|truncate)\s",
    _CMD + r"dd\s",
    r"\bmkfs",
    r"\bdiskutil\s+(?:erase|partition|reformat|zero|secureErase|apfs\s+delete)",
    _CMD + r"chown\s",
    _CMD + r"chmod\s+(?:-\w*R|.*\s/)",
    _CMD + r"(?:kill|killall|pkill)\s",
    _CMD + r"(?:shutdown|reboot|halt)\b",
    r"\bgit\s+push\b",
    r"\bgit\s+reset\s+--hard",
    r"\bgit\s+clean\b",
    r"\bgit\s+checkout\s+(?:--\s|\.\s*$)",
    r"\bgit\s+branch\s+-D\b",
    r"\bgit\s+(?:rebase|filter-branch)\b",
    r"\bgit\s+stash\s+(?:drop|clear)\b",
    r"\b(?:curl|wget)\b[^|]*\|\s*(?:sudo\s+)?(?:ba|z|da|k)?sh\b",
    r"\blaunchctl\b",
    r"\bdefaults\s+(?:write|delete)\b",
    r"\bcrontab\b",
    r"\b(?:npm|yarn|pnpm)\s+publish\b",
    r"\btwine\s+upload\b",
    r"\bbrew\s+(?:uninstall|remove|rm)\b",
    r"\bpip3?\s+uninstall\b",
    r"\bfind\b[^|;]*\s-delete\b",
    r"\bfind\b[^|;]*-exec\s+rm\b",
    r"\bxargs\b[^|;]*\brm\b",
    r":\(\)\s*\{",
    r"\b(?:networksetup|scutil|csrutil|nvram|pmset|systemsetup|spctl|tccutil|fdesetup|dscl)\b",
    r"\btmutil\s+(?:delete|disable|deletelocalsnapshots)",
    r"\bsecurity\s+(?:delete|dump|find-generic-password|find-internet-password)",
    r"\bosascript\b",
    _CMD + r"mv\s[^|;&]*\s[~/]",
    _CMD + r"mv\s+[~/]",
    r"(?:^|[^>&0-9])>\s*(?!/dev/null)(?!/tmp/)(?!/private/tmp/)[~/]",
    r"\bhistory\s+-c\b",
]
_RISKY_SHELL = [re.compile(p) for p in RISKY_SHELL_PATTERNS]

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")
BACKGROUND_JOBS: dict[int, tuple[str, Path, subprocess.Popen]] = {}  # pid -> (command, log, process)


def is_risky_command(command: str) -> bool:
    return any(p.search(command) for p in _RISKY_SHELL)


_KILL = re.compile(r"^\s*kill\s+(?:-\w+\s+)?(\d+)\s*$")


def stops_own_job(command: str) -> bool:
    """`kill <pid>` for a background job Jarvis started itself (like its own dev server) is always fine."""
    match = _KILL.match(command)
    return bool(match) and int(match.group(1)) in BACKGROUND_JOBS


def needs_confirmation(ctx: ToolContext, command: str) -> bool:
    mode = str(getattr(ctx.cfg.safety, "confirm_shell", "risky")).lower()
    if mode == "always":
        return True
    if mode == "never":
        return False
    return is_risky_command(command) and not stops_own_job(command)


def shell_path() -> str:
    for candidate in ("/bin/zsh", "/bin/bash", "/usr/bin/bash", "/bin/sh"):
        if os.path.exists(candidate):
            return candidate
    return "/bin/sh"


def shell_env() -> dict:
    env = dict(os.environ)
    parts = [p for p in env.get("PATH", "").split(":") if p]
    venv = env.pop("VIRTUAL_ENV", None)
    if venv:  # never leak Jarvis's own virtualenv into the user's commands
        parts = [p for p in parts if not p.startswith(venv)]
    for extra in ("/usr/local/bin", "/usr/local/sbin", "/opt/homebrew/bin", "/opt/homebrew/sbin",
                  str(Path.home() / ".local/bin"), "/usr/bin", "/bin", "/usr/sbin", "/sbin"):
        if extra not in parts and os.path.isdir(extra):
            parts.append(extra)
    env["PATH"] = ":".join(parts)
    for key in ("PYTHONHOME", "PYTHONPATH", "__PYVENV_LAUNCHER__"):
        env.pop(key, None)
    env.update({
        "PAGER": "cat", "GIT_PAGER": "cat", "MANPAGER": "cat", "GIT_TERMINAL_PROMPT": "0",
        "HOMEBREW_NO_AUTO_UPDATE": "1", "HOMEBREW_NO_INSTALL_CLEANUP": "1", "PYTHONUNBUFFERED": "1",
        "NO_COLOR": "1", "CLICOLOR": "0", "TERM": "dumb", "CI": env.get("CI", ""),
        "npm_config_yes": "true",
    })
    if not env["CI"]:
        env.pop("CI")
    return env


def clean_output(text: str) -> str:
    text = ANSI_RE.sub("", text)
    # Progress bars redraw with \r: keep only the final state of each line.
    return "\n".join(line.split("\r")[-1] for line in text.split("\n"))


def _kill_tree(proc: subprocess.Popen) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            return
        try:
            proc.wait(timeout=1.5)
            return
        except subprocess.TimeoutExpired:
            continue


class _OutputCollector:
    """Reads a pipe in a thread, keeping the head and tail of very long output."""

    HEAD_LIMIT = 200_000

    def __init__(self, pipe) -> None:
        self.pipe = pipe
        self.head = bytearray()
        self.tail: collections.deque = collections.deque(maxlen=64)
        self.dropped = 0
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        reader = getattr(self.pipe, "read1", self.pipe.read)
        try:
            while True:
                chunk = reader(65536)
                if not chunk:
                    break
                if len(self.head) < self.HEAD_LIMIT:
                    self.head.extend(chunk)
                else:
                    if len(self.tail) == self.tail.maxlen:
                        self.dropped += len(self.tail[0])
                    self.tail.append(chunk)
        except (OSError, ValueError):
            pass

    def text(self) -> str:
        data = bytes(self.head)
        if self.tail:
            if self.dropped:
                data += f"\n[... {self.dropped} bytes omitted ...]\n".encode()
            data += b"".join(self.tail)
        return clean_output(data.decode("utf-8", errors="replace"))


def _close_pipe(proc: subprocess.Popen, collector: "_OutputCollector") -> None:
    """Close our end of the output pipe, but only once it has been read to the end: closing it while a
    background child still writes to it could kill that child (a server started with &)."""
    collector.thread.join(0.5)
    if not collector.thread.is_alive():
        try:
            proc.stdout.close()
        except (OSError, ValueError):
            pass


def _run_foreground(ctx: ToolContext, argv: list[str], cwd: Path, env: dict, timeout: float) -> ToolResult:
    proc = subprocess.Popen(argv, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, start_new_session=True)
    collector = _OutputCollector(proc.stdout)
    deadline = time.monotonic() + timeout
    exited_at = None
    timed_out = lingering = False
    while True:
        if ctx.cancel.is_set():
            _kill_tree(proc)
            _close_pipe(proc, collector)
            raise Cancelled()
        if proc.poll() is not None:
            exited_at = exited_at or time.monotonic()
            collector.thread.join(0.1)
            if not collector.thread.is_alive():
                break
            if time.monotonic() - exited_at > 2.0:  # a background child still holds the output pipe
                lingering = True
                break
        elif time.monotonic() > deadline:
            timed_out = True
            _kill_tree(proc)
            collector.thread.join(2.0)
            break
        else:
            time.sleep(0.05)
    output = collector.text().strip()
    _close_pipe(proc, collector)
    rc = proc.returncode
    if timed_out:
        header = f"Timed out after {timeout:.0f}s and was stopped. For servers or long jobs use background=true."
    else:
        header = f"exit code {rc}"
        if lingering:
            header += " (a background process it started is still running)"
    return ToolResult(f"{header}\n{output or '(no output)'}", is_error=bool(timed_out or (rc not in (0, None))))


def _run_background(ctx: ToolContext, argv: list[str], cwd: Path, env: dict, command: str) -> ToolResult:
    log_dir = Path(ctx.cfg.paths.logs) / "background"
    log_dir.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^A-Za-z0-9]+", "-", command).strip("-")[:40] or "job"
    log_path = log_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{slug}.log"
    with open(log_path, "wb") as log_file:
        proc = subprocess.Popen(argv, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL, stdout=log_file,
                                stderr=subprocess.STDOUT, start_new_session=True)
    for _ in range(25):  # give it ~2.5 s to fail fast or print a URL
        if proc.poll() is not None or ctx.cancel.is_set():
            break
        time.sleep(0.1)
    output = clean_output(log_path.read_text(encoding="utf-8", errors="replace"))[-4000:].strip()
    rc = proc.poll()
    if rc is not None:
        return ToolResult(f"The command exited right away with code {rc}.\n{output or '(no output)'}",
                          is_error=rc != 0)
    BACKGROUND_JOBS[proc.pid] = (command, log_path, proc)
    return ToolResult(
        f"Running in the background (pid {proc.pid}).\nOutput so far:\n{output or '(nothing yet)'}\n"
        f"Full log: {log_path}\nTo stop it: kill {proc.pid}"
    )


def run_shell(ctx: ToolContext, a: dict) -> ToolResult:
    command = str(require(a, "command"))
    cwd = resolve_path(ctx, a["cwd"]) if a.get("cwd") else ctx.workspace
    if not cwd.is_dir():
        raise ToolError(f"Working folder doesn't exist: {cwd}")
    try:
        timeout = float(a.get("timeout") or ctx.cfg.safety.shell_timeout_seconds)
    except (TypeError, ValueError):
        timeout = float(ctx.cfg.safety.shell_timeout_seconds)
    timeout = max(1.0, min(timeout, 1800.0))
    if needs_confirmation(ctx, command):
        if not ctx.confirm("Jarvis wants to run a command", f"{command}\n\nin {cwd}"):
            raise ToolError("The user declined to run this command. Don't retry it; ask or choose a safer approach.")
    ctx.check_cancel()
    argv = [shell_path(), "-c", command]
    env = shell_env()
    if a.get("background"):
        return _run_background(ctx, argv, cwd, env, command)
    return _run_foreground(ctx, argv, cwd, env, timeout)


def register(reg) -> None:
    reg.add(
        "run_shell",
        """Run a shell command (zsh) on the user's Mac and get its output and exit code. The working folder
        defaults to the workspace. No interactive input is possible (stdin is closed), so use non-interactive
        flags (-y, --yes). Risky commands (deleting, sudo, git push, system settings...) are shown to the user
        for approval first. For servers or anything long-running set background=true: it keeps running and
        you get the pid and a log file.""",
        {
            "command": {"type": "string", "description": "The command line to run."},
            "cwd": {"type": "string", "description": "Working folder (default: the workspace)."},
            "timeout": {"type": "number", "description": "Seconds before it is stopped (default 120, max 1800)."},
            "background": {"type": "boolean", "description": "Start it and return immediately (servers, watchers)."},
        },
        required=("command",), func=run_shell,
        status=lambda a: ("Starting: " if a.get("background") else "Running: ") + short(a.get("command", ""), 80),
        activity="Preparing a command…",
    )
