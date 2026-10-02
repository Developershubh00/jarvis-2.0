"""The menu-bar app: a status-bar icon and menu plus global hotkeys, wired to the assistant engine.

Phase 7 shows Jarvis's state through the menu bar (icon and status line) and notifications. The floating
glass panel arrives in phase 8 and the tutor pointer in phase 9; both plug into the same bridge.
"""
from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import AppKit
from AppKit import NSApplication, NSImage, NSMenu, NSMenuItem, NSStatusBar
from Foundation import NSObject
from PyObjCTools import AppHelper

from .. import mac
from ..assistant import Assistant
from . import UIBridge
from .hotkeys import PTT_DISPLAY, Hotkey, HotkeyError, HotkeyManager, parse_hotkey, parse_ptt
from .menu_panel import MenuPanel, NullOverlay, short

log = logging.getLogger(__name__)

ACCESSORY_POLICY = getattr(AppKit, "NSApplicationActivationPolicyAccessory", 1)
STATE_SYMBOLS = {
    "idle": "waveform.circle", "listening": "mic.circle.fill", "transcribing": "ellipsis.circle",
    "thinking": "sparkles", "speaking": "speaker.wave.2.circle", "waiting": "hand.raised.circle",
    "error": "exclamationmark.triangle",
}
DEFAULT_KEYS = {"talk": "ctrl+alt+c", "tutor": "ctrl+alt+t", "type": "ctrl+alt+j"}
ACTIONS = ("talk", "type")  # the tutor hotkey arrives with tutor mode in phase 9
FUNCTION_KEY_CHARS = {f"f{i}": chr(0xF704 + i - 1) for i in range(1, 21)}


def guard(fn, *args) -> None:
    try:
        fn(*args)
    except Exception:
        log.exception("UI call %s failed", getattr(fn, "__name__", fn))


def dispatch(target, name: str) -> None:
    controller = getattr(target, "jcontroller", None)
    if controller is not None:
        guard(getattr(controller, name))


class JarvisMenuTarget(NSObject):
    """Receives menu clicks and forwards them to the Python controller."""

    def talk_(self, sender):
        dispatch(self, "menu_talk")

    def typeCommand_(self, sender):
        dispatch(self, "menu_type")

    def stopNow_(self, sender):
        dispatch(self, "menu_stop")

    def copyLast_(self, sender):
        dispatch(self, "menu_copy_last")

    def newConversation_(self, sender):
        dispatch(self, "menu_new_conversation")

    def openWorkspace_(self, sender):
        dispatch(self, "menu_open_workspace")

    def openConfig_(self, sender):
        dispatch(self, "menu_open_config")

    def openLog_(self, sender):
        dispatch(self, "menu_open_log")

    def quitJarvis_(self, sender):
        dispatch(self, "menu_quit")


class AppUIBridge(UIBridge):
    """Thread-safe bridge: every call is forwarded to the main thread."""

    def __init__(self, app: "JarvisApp") -> None:
        self.app = app

    def _main(self, fn, *args) -> None:
        AppHelper.callAfter(guard, fn, *args)

    def set_state(self, state, title=None, subtitle=None) -> None:
        self._main(self.app.apply_state, state, title, subtitle)

    def set_level(self, level) -> None:
        self._main(self.app.hud.set_level, level)

    def set_activity(self, text) -> None:
        self._main(self.app.hud.set_subtitle, text)

    def begin_turn(self) -> None:
        self._main(self.app.hud.clear_text)

    def show_user_text(self, text) -> None:
        self._main(self.app.hud.set_subtitle, f"“{text}”")

    def append_text(self, text) -> None:
        self._main(self.app.hud.append, text, "response")

    def show_status(self, text) -> None:
        self._main(self.app.hud.append, text, "status")

    def show_error(self, text) -> None:
        self._main(self.app.hud.append, text, "error")

    def show_response(self, text) -> None:
        self.app.last_response = text

        def ensure_visible() -> None:
            if self.app.hud.text_is_empty() and text:
                self.app.hud.append(text, "response")
            done = getattr(self.app.hud, "reply_done", None)
            if done is not None:
                done(text)

        self._main(ensure_visible)

    def hint(self, text) -> None:
        self._main(self.app.show_hint, text)

    def schedule_hide(self, seconds=None) -> None:
        delay = float(self.app.cfg.ui.hud_autohide_seconds if seconds is None else seconds)
        self._main(self.app.hud.schedule_hide, delay)

    def point_at(self, x, y, label) -> None:
        self._main(self.app.overlay.point_at, x, y, label)

    def hide_pointer(self) -> None:
        self._main(self.app.overlay.hide)

    def hide_pointer_after(self, seconds) -> None:
        self._main(self.app.overlay.hide_after, seconds)

    @contextmanager
    def hidden_for_capture(self) -> Iterator[None]:
        done = threading.Event()

        def hide() -> None:
            self.app.hud.set_capture_hidden(True)
            self.app.overlay.set_capture_hidden(True)
            done.set()

        AppHelper.callAfter(guard, hide)
        done.wait(1.0)
        time.sleep(0.15)  # let the window server redraw without our windows
        try:
            yield
        finally:
            def show() -> None:
                self.app.hud.set_capture_hidden(False)
                self.app.overlay.set_capture_hidden(False)

            AppHelper.callAfter(guard, show)

    def confirm(self, title, message) -> bool:
        previous = self.app.assistant.state
        self.app.assistant._set_state("waiting", "Waiting for your OK", "Check the dialog on screen.")
        try:
            return mac.confirm_dialog(title, message, cancel=self.app.assistant.cancel)
        finally:
            self.app.assistant._set_state(previous)

    def ask_text(self, prompt) -> str | None:
        return mac.ask_text_dialog(prompt, title=str(self.app.cfg.assistant_name))


class JarvisApp:
    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.last_response = ""
        self.warnings: list[str] = list(getattr(cfg, "warnings", []))
        self.bindings: dict[str, Hotkey] = {}
        self.ptt: str | None = None
        self.hotkeys: HotkeyManager | None = None
        self.hotkey_attempts = 0

    # ------------------------------------------------------------ setup

    def build(self, preload: bool = True) -> None:
        self.nsapp = NSApplication.sharedApplication()
        self.nsapp.setActivationPolicy_(ACCESSORY_POLICY)
        self._parse_hotkeys()
        self.hud = MenuPanel(set_status=self.set_status_line, notify=self.notify, name=str(self.cfg.assistant_name),
                             idle_hint=self.idle_hint(), notify_replies=not bool(self.cfg.voice.tts))
        self.overlay = NullOverlay()
        self.ui = AppUIBridge(self)
        self.assistant = Assistant(self.cfg, self.ui)
        self._build_status_item()
        self.assistant.start(preload=preload)
        self._install_hotkeys()
        self._welcome()

    def _parse_hotkeys(self) -> None:
        for action in ACTIONS:
            spec = getattr(self.cfg.hotkeys, action, "")
            if not spec:
                continue
            try:
                self.bindings[action] = parse_hotkey(spec)
            except HotkeyError as e:
                self.warnings.append(f"Hotkey for {action}: {e} Using {DEFAULT_KEYS[action]}.")
                self.bindings[action] = parse_hotkey(DEFAULT_KEYS[action])
        try:
            self.ptt = parse_ptt(self.cfg.hotkeys.push_to_talk)
        except HotkeyError as e:
            self.warnings.append(str(e))
            self.ptt = None

    def idle_hint(self) -> str:
        parts = []
        if "talk" in self.bindings:
            parts.append(f"Press {self.bindings['talk'].pretty()}")
        if self.ptt:
            parts.append(f"hold {PTT_DISPLAY[self.ptt]}")
        start = " or ".join(parts) if parts else "Click the menu bar icon"
        typing = f" {self.bindings['type'].pretty()} to type instead." if "type" in self.bindings else ""
        return f"{start} and speak.{typing}"

    def _build_status_item(self) -> None:
        self.status_item = NSStatusBar.systemStatusBar().statusItemWithLength_(-1)
        self.menu_target = JarvisMenuTarget.alloc().init()
        self.menu_target.jcontroller = self
        menu = NSMenu.alloc().init()
        menu.setAutoenablesItems_(False)
        self.status_line_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Starting…", None, "")
        self.status_line_item.setEnabled_(False)
        menu.addItem_(self.status_line_item)
        menu.addItem_(NSMenuItem.separatorItem())

        def item(title: str, action: str, hotkey: Hotkey | None = None) -> None:
            entry = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, "")
            entry.setTarget_(self.menu_target)
            if hotkey is not None:
                key = FUNCTION_KEY_CHARS.get(hotkey.key) or {"space": " ", "return": "\r", "tab": "\t"}.get(
                    hotkey.key, hotkey.key if len(hotkey.key) == 1 else "")
                if key:
                    entry.setKeyEquivalent_(key)
                    entry.setKeyEquivalentModifierMask_(hotkey.mods)
            menu.addItem_(entry)

        item("Talk", "talk:", self.bindings.get("talk"))
        item("Type a request…", "typeCommand:", self.bindings.get("type"))
        item("Stop", "stopNow:")
        menu.addItem_(NSMenuItem.separatorItem())
        item("Copy last reply", "copyLast:")
        item("New conversation", "newConversation:")
        menu.addItem_(NSMenuItem.separatorItem())
        item("Open workspace folder", "openWorkspace:")
        item("Edit settings", "openConfig:")
        item("Open log", "openLog:")
        menu.addItem_(NSMenuItem.separatorItem())
        item("Quit Jarvis", "quitJarvis:")
        self.status_item.setMenu_(menu)
        self.menu = menu
        self.set_icon("idle")

    def set_status_line(self, text: str) -> None:
        self.status_line_item.setTitle_(short(text, 64))
        button = self.status_item.button()
        if button is not None:
            button.setToolTip_(f"{self.cfg.assistant_name}: {text}")

    def notify(self, title: str, message: str) -> None:
        threading.Thread(target=mac.notify, args=(title, message), daemon=True).start()

    def set_icon(self, state: str) -> None:
        button = self.status_item.button()
        if button is None:
            return
        image = None
        try:
            image = NSImage.imageWithSystemSymbolName_accessibilityDescription_(
                STATE_SYMBOLS.get(state, "waveform.circle"), "Jarvis")
        except Exception:
            image = None
        if image is not None:
            image.setTemplate_(True)
            button.setImage_(image)
            button.setTitle_("")
        else:
            button.setTitle_("J")

    def _install_hotkeys(self) -> None:
        if self.hotkeys is None:
            self.hotkeys = HotkeyManager(self.bindings, on_hotkey=self.on_hotkey, on_escape=self.assistant.on_cancel,
                                         ptt=self.ptt, on_ptt_down=self.assistant.on_ptt_down,
                                         on_ptt_up=self.assistant.on_ptt_up)
        try:
            ok = self.hotkeys.start()
        except Exception:
            log.exception("Couldn't start the hotkey listener")
            ok = False
        if ok:
            if self.hotkey_attempts > 0:
                mac.notify(str(self.cfg.assistant_name), "Hotkeys are working now.")
            return
        self.hotkey_attempts += 1
        if self.hotkey_attempts == 1:
            mac.accessibility_trusted(prompt=True)
            self.apply_state("error", "Hotkeys need Accessibility permission",
                             "System Settings > Privacy & Security > Accessibility: turn on your terminal app. "
                             "The menu bar icon works meanwhile.")
        if self.hotkey_attempts < 100:  # keep retrying for about five minutes
            AppHelper.callLater(3.0, guard, self._install_hotkeys)

    def _welcome(self) -> None:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            self.apply_state("error", "Add your API key", "Put ANTHROPIC_API_KEY in the .env file in the Jarvis "
                             "folder, then restart Jarvis.")
            self.hud.schedule_hide(30)
            return
        if self.warnings:
            self.apply_state("error", "Check your settings", self.warnings[0])
            for warning in self.warnings:
                self.hud.append(warning, "error")
            self.hud.schedule_hide(20)
            return
        if self.hotkeys is not None and self.hotkeys.tap is not None:
            self.apply_state("idle", f"{self.cfg.assistant_name} is ready", self.idle_hint())
            self.hud.show()
            self.apply_state("idle")

    # ------------------------------------------------------------ state, hints

    def apply_state(self, state, title=None, subtitle=None) -> None:
        self.hud.set_state(state, title, subtitle)
        self.set_icon(state)

    def show_hint(self, text: str) -> None:
        self.hud.set_subtitle(text)
        self.hud.show()
        if self.assistant.state in ("idle", "error"):
            self.hud.schedule_hide(4)

    # ------------------------------------------------------------ hotkeys (main thread)

    def on_hotkey(self, action: str) -> None:
        if action == "talk":
            self.assistant.on_talk(tutor=False)
        elif action == "tutor":
            self.assistant.on_talk(tutor=True)
        elif action == "type":
            self.assistant.on_type()

    # ------------------------------------------------------------ menu actions

    def menu_talk(self) -> None:
        self.assistant.on_talk(tutor=False)

    def menu_type(self) -> None:
        self.assistant.on_type()

    def menu_stop(self) -> None:
        self.assistant._last_escape = time.monotonic()  # menu Stop needs no double press
        self.assistant.on_cancel()

    def menu_copy_last(self) -> None:
        if self.last_response:
            mac.set_clipboard(self.last_response)
            self.show_hint("Copied my last reply.")
        else:
            self.show_hint("Nothing to copy yet.")

    def menu_new_conversation(self) -> None:
        self.assistant.new_conversation()

    def menu_open_workspace(self) -> None:
        subprocess.Popen(["open", str(self.cfg.paths.workspace)])

    def menu_open_config(self) -> None:
        local = Path(self.cfg.paths.local_config_file)
        if not local.exists():
            local.write_text("# Your personal Jarvis settings. Copy settings from config.yaml and change them here.\n",
                             encoding="utf-8")
        subprocess.Popen(["open", "-t", str(local)])

    def menu_open_log(self) -> None:
        log_file = Path(self.cfg.paths.logs) / "jarvis.log"
        subprocess.Popen(["open", "-t", str(log_file)])

    def menu_quit(self) -> None:
        try:
            if self.hotkeys is not None:
                self.hotkeys.stop()
            self.assistant.shutdown()
        finally:
            AppHelper.stopEventLoop()


def run_app(cfg) -> int:
    app = JarvisApp(cfg)
    app.build()
    log.info("Jarvis is running (model %s)", cfg.llm.model)
    talk = app.bindings.get("talk")
    print(f"{cfg.assistant_name} is running in the menu bar. "
          f"{'Press ' + talk.pretty() + ' in any app and speak.' if talk else 'Click its menu bar icon.'}")
    print("Keep this Terminal window open; press Ctrl+C here (or use the menu) to quit.")
    for warning in app.warnings:
        print(f"Warning: {warning}")
    AppHelper.runEventLoop(installInterrupt=True)
    return 0
