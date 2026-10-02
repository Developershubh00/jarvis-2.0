"""Start Jarvis when you log in.

./run.sh --login on adds Jarvis.command (in your Jarvis folder) to your Login Items. At login it opens
Terminal and runs Jarvis there, so Jarvis keeps Terminal's permissions (Accessibility, Microphone,
Screen Recording) instead of needing them all again as a separate background app.
"""
from __future__ import annotations

import shlex
import sys
from pathlib import Path

from . import mac

COMMAND_NAME = "Jarvis.command"
BY_HAND = ("To do it by hand: open System Settings, General, Login Items, click + under Open at Login, "
           "and choose Jarvis.command in your Jarvis folder.")


def command_path(cfg) -> Path:
    return Path(cfg.paths.project_root) / COMMAND_NAME


def write_command(cfg) -> Path:
    path = command_path(cfg)
    root = shlex.quote(str(Path(cfg.paths.project_root)))
    path.write_text("#!/bin/bash\n"
                    "# Starts Jarvis in the menu bar. Created by ./run.sh --login on (double-clicking works too).\n"
                    f"cd {root} && exec ./run.sh\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def login_items_script(path: Path, action: str) -> str:
    quoted = mac.as_quote(str(path))
    if action == "add":
        return ('tell application "System Events"\n'
                f"    delete (every login item whose path is {quoted})\n"
                f"    make login item at end with properties {{path:{quoted}, hidden:false}}\n"
                "end tell")
    if action == "remove":
        return f'tell application "System Events" to delete (every login item whose path is {quoted})'
    return 'tell application "System Events" to get the path of every login item'


def set_login(cfg, on: bool) -> tuple[bool, str]:
    """Turn starting at login on or off. Returns (worked, message for the user)."""
    if sys.platform != "darwin":
        return False, "Starting at login needs macOS."
    path = write_command(cfg) if on else command_path(cfg)
    try:
        mac.osascript(login_items_script(path, "add" if on else "remove"), timeout=30)
    except mac.AppleScriptError as e:
        message = f"Couldn't change your Login Items: {e}."
        if "-1743" in str(e) or "Not authorized" in str(e):
            message += (" Allow your terminal app to control System Events in System Settings, Privacy & "
                        "Security, Automation, then try again.")
        return False, f"{message} {BY_HAND}" if on else message
    if on:
        return True, (f"Jarvis will start when you log in: it opens Terminal and runs {path.name}. "
                      "Turn it off with ./run.sh --login off.")
    return True, "Jarvis won't start at login any more."


def login_status(cfg) -> bool | None:
    """True or False, or None when it can't be checked (not macOS, or no permission to ask)."""
    if sys.platform != "darwin":
        return None
    try:
        listing = mac.osascript(login_items_script(command_path(cfg), "list"), timeout=30)
    except mac.AppleScriptError:
        return None
    return str(command_path(cfg)) in listing
