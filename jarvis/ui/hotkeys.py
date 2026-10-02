"""Global hotkeys through a Quartz event tap (needs Accessibility permission).

Parsing is pure Python so it can be tested anywhere; Quartz is imported only when the tap starts.
Matching hotkeys are swallowed so the letter doesn't also get typed into the app you're in.
Esc is observed (never swallowed). Push-to-talk watches a single modifier key being held.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

log = logging.getLogger(__name__)

MOD_SHIFT = 0x20000
MOD_CTRL = 0x40000
MOD_ALT = 0x80000
MOD_CMD = 0x100000
MOD_FN = 0x800000
MOD_MASK = MOD_SHIFT | MOD_CTRL | MOD_ALT | MOD_CMD

MOD_NAMES = {
    "ctrl": MOD_CTRL, "control": MOD_CTRL, "ctl": MOD_CTRL, "⌃": MOD_CTRL,
    "alt": MOD_ALT, "option": MOD_ALT, "opt": MOD_ALT, "⌥": MOD_ALT,
    "cmd": MOD_CMD, "command": MOD_CMD, "⌘": MOD_CMD,
    "shift": MOD_SHIFT, "⇧": MOD_SHIFT,
}

# macOS virtual key codes (ANSI layout).
KEYCODES = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9, "b": 11, "q": 12,
    "w": 13, "e": 14, "r": 15, "y": 16, "t": 17, "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23,
    "=": 24, "9": 25, "7": 26, "-": 27, "8": 28, "0": 29, "]": 30, "o": 31, "u": 32, "[": 33, "i": 34,
    "p": 35, "return": 36, "l": 37, "j": 38, "'": 39, "k": 40, ";": 41, "\\": 42, ",": 43, "/": 44,
    "n": 45, "m": 46, ".": 47, "tab": 48, "space": 49, "`": 50, "delete": 51, "escape": 53,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97, "f7": 98, "f8": 100, "f9": 101,
    "f10": 109, "f11": 103, "f12": 111, "f13": 105, "f14": 107, "f15": 113, "f16": 106, "f17": 64,
    "f18": 79, "f19": 80, "f20": 90, "home": 115, "end": 119, "pageup": 116, "pagedown": 121,
    "forwarddelete": 117, "left": 123, "right": 124, "down": 125, "up": 126,
}
ALIASES = {
    "esc": "escape", "enter": "return", "spacebar": "space", "backspace": "delete", "del": "forwarddelete",
    "minus": "-", "equals": "=", "equal": "=", "comma": ",", "period": ".", "dot": ".", "slash": "/",
    "backslash": "\\", "semicolon": ";", "quote": "'", "backtick": "`", "grave": "`",
    "leftbracket": "[", "rightbracket": "]", "pgup": "pageup", "pgdn": "pagedown",
}
TYPING_KEYS = set("abcdefghijklmnopqrstuvwxyz0123456789=-][';\\,./`") | {"space", "return", "tab", "delete"}
DISPLAY = {"return": "↩", "tab": "⇥", "space": "Space", "delete": "⌫", "escape": "Esc", "left": "←",
           "right": "→", "up": "↑", "down": "↓", "forwarddelete": "⌦", "pageup": "PgUp", "pagedown": "PgDn",
           "home": "Home", "end": "End"}

# Push-to-talk keys: (keycode, flag bit that is set while the key is down).
PTT_KEYS = {
    "right_option": (61, 0x40), "left_option": (58, 0x20),
    "right_command": (54, 0x10), "right_control": (62, 0x2000),
    "right_shift": (60, 0x04), "fn": (63, MOD_FN),
}
PTT_OWN_MOD = {"right_option": MOD_ALT, "left_option": MOD_ALT, "right_command": MOD_CMD,
               "right_control": MOD_CTRL, "right_shift": MOD_SHIFT, "fn": MOD_FN}
PTT_DISPLAY = {"right_option": "right ⌥ Option", "left_option": "left ⌥ Option", "right_command": "right ⌘ Command",
               "right_control": "right ⌃ Control", "right_shift": "right ⇧ Shift", "fn": "fn"}

KEY_DOWN, KEY_UP, FLAGS_CHANGED = 10, 11, 12
MOUSE_DOWNS = (1, 3, 25)
ESCAPE = 53
TAP_DISABLED = (0xFFFFFFFE, 0xFFFFFFFF, -2, -1)


class HotkeyError(ValueError):
    pass


@dataclass(frozen=True)
class Hotkey:
    keycode: int
    mods: int
    key: str

    def matches(self, keycode: int, flags: int) -> bool:
        return keycode == self.keycode and (flags & MOD_MASK) == self.mods

    def pretty(self) -> str:
        symbols = "".join(sym for bit, sym in ((MOD_CTRL, "⌃"), (MOD_ALT, "⌥"), (MOD_SHIFT, "⇧"), (MOD_CMD, "⌘"))
                          if self.mods & bit)
        key = DISPLAY.get(self.key, self.key.upper())
        return symbols + key


def parse_hotkey(spec: str) -> Hotkey:
    s = str(spec or "").strip().lower().replace(" ", "")
    if not s:
        raise HotkeyError("empty hotkey")
    parts = s.split("+")
    key = ALIASES.get(parts[-1], parts[-1])
    mods = 0
    for part in parts[:-1]:
        if part not in MOD_NAMES:
            raise HotkeyError(f"Unknown modifier '{part}' in '{spec}'. Use ctrl, alt (option), cmd or shift.")
        mods |= MOD_NAMES[part]
    if key not in KEYCODES:
        raise HotkeyError(f"Unknown key '{parts[-1]}' in '{spec}'.")
    if key in TYPING_KEYS and mods in (0, MOD_SHIFT):
        raise HotkeyError(f"'{spec}' would fire every time you type it. Add modifiers (like ctrl+alt+{key}) "
                          "or use a function key such as f5.")
    return Hotkey(KEYCODES[key], mods, key)


def parse_ptt(spec: str) -> str | None:
    s = str(spec or "").strip().lower().replace(" ", "_").replace("-", "_")
    if s in ("", "none", "off", "false", "no"):
        return None
    s = {"right_alt": "right_option", "left_alt": "left_option", "right_cmd": "right_command",
         "right_ctrl": "right_control", "globe": "fn"}.get(s, s)
    if s not in PTT_KEYS:
        raise HotkeyError(f"Unknown push-to-talk key '{spec}'. Options: {', '.join(PTT_KEYS)}.")
    return s


class HotkeyManager:
    HOLD_DELAY = 0.3

    def __init__(self, bindings: dict[str, Hotkey], on_hotkey: Callable[[str], None],
                 on_escape: Callable[[], None] | None = None, ptt: str | None = None,
                 on_ptt_down: Callable[[], None] | None = None, on_ptt_up: Callable[[], None] | None = None) -> None:
        self.bindings = dict(bindings)
        self.on_hotkey = on_hotkey
        self.on_escape = on_escape
        self.ptt = ptt
        self.on_ptt_down = on_ptt_down
        self.on_ptt_up = on_ptt_up
        self.tap = None
        self.source = None
        self.listen_only = False
        self._swallow_up: set[int] = set()
        self._ptt_held = False
        self._ptt_active = False
        self._ptt_token = 0
        self._callback_ref = self._callback  # keep a strong reference for the C callback

    # ------------------------------------------------------------ setup

    def start(self) -> bool:
        import Quartz

        mask = 0
        for etype in (KEY_DOWN, KEY_UP, FLAGS_CHANGED) + MOUSE_DOWNS:
            mask |= 1 << etype
        tap = Quartz.CGEventTapCreate(Quartz.kCGSessionEventTap, Quartz.kCGHeadInsertEventTap,
                                      Quartz.kCGEventTapOptionDefault, mask, self._callback_ref, None)
        if tap is None:
            tap = Quartz.CGEventTapCreate(Quartz.kCGSessionEventTap, Quartz.kCGHeadInsertEventTap,
                                          Quartz.kCGEventTapOptionListenOnly, mask, self._callback_ref, None)
            self.listen_only = tap is not None
        if tap is None:
            return False
        self.tap = tap
        self.source = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
        Quartz.CFRunLoopAddSource(Quartz.CFRunLoopGetMain(), self.source, Quartz.kCFRunLoopCommonModes)
        Quartz.CGEventTapEnable(tap, True)
        log.info("Hotkeys active%s", " (listen-only: keys also reach the app)" if self.listen_only else "")
        return True

    def stop(self) -> None:
        if self.tap is None:
            return
        try:
            import Quartz

            Quartz.CGEventTapEnable(self.tap, False)
            if self.source is not None:
                Quartz.CFRunLoopRemoveSource(Quartz.CFRunLoopGetMain(), self.source, Quartz.kCFRunLoopCommonModes)
        except Exception:
            pass
        self.tap = None
        self.source = None

    # ------------------------------------------------------------ event handling (main thread)

    def _callback(self, proxy, etype, event, refcon):
        try:
            return self._handle(int(etype), event)
        except Exception:
            log.exception("Hotkey callback failed")
            return event

    def _dispatch(self, fn: Callable | None, *args) -> None:
        if fn is None:
            return
        from PyObjCTools import AppHelper

        AppHelper.callAfter(fn, *args)

    def _handle(self, etype: int, event):
        import Quartz

        if etype in TAP_DISABLED:
            if self.tap is not None:
                Quartz.CGEventTapEnable(self.tap, True)
            return event
        if etype in MOUSE_DOWNS:
            self._cancel_pending_ptt()
            return event
        if etype == FLAGS_CHANGED:
            self._handle_flags(event)
            return event
        if etype not in (KEY_DOWN, KEY_UP):
            return event
        keycode = int(Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode))
        if etype == KEY_DOWN:
            self._cancel_pending_ptt()
            repeat = bool(Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventAutorepeat))
            if keycode == ESCAPE and not repeat:
                self._dispatch(self.on_escape)
                return event
            flags = int(Quartz.CGEventGetFlags(event))
            for action, hotkey in self.bindings.items():
                if hotkey.matches(keycode, flags):
                    if not repeat:
                        self._dispatch(self.on_hotkey, action)
                    if self.listen_only:
                        return event
                    self._swallow_up.add(keycode)
                    return None
            return event
        if keycode in self._swallow_up:  # key up of a swallowed hotkey
            self._swallow_up.discard(keycode)
            return event if self.listen_only else None
        return event

    def _handle_flags(self, event) -> None:
        if not self.ptt:
            return
        import Quartz

        keycode = int(Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode))
        flags = int(Quartz.CGEventGetFlags(event))
        ptt_code, ptt_bit = PTT_KEYS[self.ptt]
        if keycode != ptt_code:
            self._cancel_pending_ptt()  # another modifier joined: it's a shortcut, not a hold
            return
        down = bool(flags & ptt_bit)
        if down and not self._ptt_held:
            self._ptt_held = True
            others = (flags & (MOD_MASK | MOD_FN)) & ~PTT_OWN_MOD[self.ptt]
            if others:
                return
            self._ptt_token += 1
            token = self._ptt_token
            from PyObjCTools import AppHelper

            AppHelper.callLater(self.HOLD_DELAY, self._ptt_fire, token)
        elif not down and self._ptt_held:
            self._ptt_held = False
            self._ptt_token += 1
            if self._ptt_active:
                self._ptt_active = False
                self._dispatch(self.on_ptt_up)

    def _ptt_fire(self, token: int) -> None:
        if token == self._ptt_token and self._ptt_held and not self._ptt_active:
            self._ptt_active = True
            if self.on_ptt_down:
                self.on_ptt_down()

    def _cancel_pending_ptt(self) -> None:
        if self._ptt_held and not self._ptt_active:
            self._ptt_token += 1
