"""Phase 7 tests: global hotkeys, the menu-bar app's logic and its menu-bar status display.

The macOS frameworks (AppKit, Quartz) are replaced by small fakes, so these run on Linux (GitHub CI)
and never install a real keyboard hook or touch your menu bar.
Run:  python -m unittest discover -s tests -v
"""
from __future__ import annotations

import importlib
import io
import logging
import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_brain import NOWHERE  # noqa: E402

from jarvis import config as config_module  # noqa: E402
from jarvis.config import load_config  # noqa: E402
from jarvis.doctor import OK, WARN, Report, check_hotkeys  # noqa: E402
from jarvis.ui import hotkeys as hk  # noqa: E402
from jarvis.ui.hotkeys import (MOD_ALT, MOD_CMD, MOD_CTRL, MOD_FN, MOD_SHIFT, HotkeyError,  # noqa: E402
                               HotkeyManager, parse_hotkey, parse_ptt)
from jarvis.ui.menu_panel import MenuPanel  # noqa: E402

RIGHT_OPTION_DOWN = MOD_ALT | 0x40


# ---------------------------------------------------------------- fakes for the macOS frameworks

class FakeQuartz(types.ModuleType):
    kCGKeyboardEventKeycode, kCGKeyboardEventAutorepeat = 9, 8
    kCGSessionEventTap = kCGHeadInsertEventTap = 0
    kCGEventTapOptionDefault, kCGEventTapOptionListenOnly = 0, 1
    kCFRunLoopCommonModes = "common"

    def __init__(self, allow_tap=True, allow_listen=True):
        super().__init__("Quartz")
        self.allow_tap, self.allow_listen, self.enabled = allow_tap, allow_listen, []

    def CGEventTapCreate(self, where, place, option, mask, callback, refcon):
        if option == self.kCGEventTapOptionDefault and self.allow_tap:
            return "tap"
        if option == self.kCGEventTapOptionListenOnly and self.allow_listen:
            return "listen-only tap"
        return None

    def CFMachPortCreateRunLoopSource(self, allocator, tap, order):
        return "source"

    def CFRunLoopGetMain(self):
        return "main loop"

    def CFRunLoopAddSource(self, loop, source, mode):
        pass

    def CFRunLoopRemoveSource(self, loop, source, mode):
        pass

    def CGEventTapEnable(self, tap, on):
        self.enabled.append(on)

    def CGEventGetIntegerValueField(self, event, field):
        return event["keycode"] if field == self.kCGKeyboardEventKeycode else int(event.get("repeat", False))

    def CGEventGetFlags(self, event):
        return event.get("flags", 0)


class FakeAppHelper(types.ModuleType):
    def __init__(self):
        super().__init__("PyObjCTools.AppHelper")
        self.later, self.stopped = [], False

    def callAfter(self, fn, *args):
        fn(*args)

    def callLater(self, delay, fn, *args):
        self.later.append((delay, fn, args))

    def runEventLoop(self, **kwargs):
        pass

    def stopEventLoop(self):
        self.stopped = True


def fake_cocoa():
    appkit, foundation = types.ModuleType("AppKit"), types.ModuleType("Foundation")

    class NSObject:
        @classmethod
        def alloc(cls):
            return cls.__new__(cls)

        def init(self):
            return self

    class NSMenuItem(NSObject):
        def initWithTitle_action_keyEquivalent_(self, title, action, key):
            self.title, self.action, self.key, self.target, self.enabled, self.mods = title, action, key, None, True, 0
            return self

        def setTarget_(self, target):
            self.target = target

        def setEnabled_(self, enabled):
            self.enabled = enabled

        def setTitle_(self, title):
            self.title = title

        def setKeyEquivalent_(self, key):
            self.key = key

        def setKeyEquivalentModifierMask_(self, mods):
            self.mods = mods

        @classmethod
        def separatorItem(cls):
            return cls.alloc().initWithTitle_action_keyEquivalent_("-", None, "")

    class NSMenu(NSObject):
        def init(self):
            self.items = []
            return self

        def setAutoenablesItems_(self, flag):
            pass

        def addItem_(self, item):
            self.items.append(item)

    class Button:
        image = title = tooltip = None

        def setImage_(self, image):
            self.image = image

        def setTitle_(self, title):
            self.title = title

        def setToolTip_(self, tip):
            self.tooltip = tip

    class StatusItem:
        def __init__(self):
            self._button, self.menu = Button(), None

        def button(self):
            return self._button

        def setMenu_(self, menu):
            self.menu = menu

    class NSStatusBar:
        @classmethod
        def systemStatusBar(cls):
            return cls()

        def statusItemWithLength_(self, length):
            return StatusItem()

    class Image:
        def __init__(self, name):
            self.name = name

        def setTemplate_(self, flag):
            pass

    class NSImage:
        @classmethod
        def imageWithSystemSymbolName_accessibilityDescription_(cls, name, description):
            return Image(name)

    class NSApplication:
        policy = None

        @classmethod
        def sharedApplication(cls):
            return SHARED_APP

        def setActivationPolicy_(self, policy):
            self.policy = policy

    SHARED_APP = NSApplication()
    for cls in (NSMenuItem, NSMenu, NSStatusBar, NSImage, NSApplication):
        setattr(appkit, cls.__name__, cls)
    appkit.NSApplicationActivationPolicyAccessory = 1
    foundation.NSObject = NSObject
    return appkit, foundation


def frameworks(quartz, helper):
    appkit, foundation = fake_cocoa()
    package = types.ModuleType("PyObjCTools")
    package.AppHelper = helper
    return {"AppKit": appkit, "Foundation": foundation, "PyObjCTools": package,
            "PyObjCTools.AppHelper": helper, "Quartz": quartz}


def key(code, flags=0, repeat=False):
    return {"keycode": code, "flags": flags, "repeat": repeat}


# ---------------------------------------------------------------- tests

class HotkeyParsingTests(unittest.TestCase):
    def test_parse(self):
        talk = parse_hotkey("ctrl+alt+c")
        self.assertEqual((talk.keycode, talk.mods), (8, MOD_CTRL | MOD_ALT))
        self.assertEqual(talk.pretty(), "⌃⌥C")
        self.assertTrue(talk.matches(8, MOD_CTRL | MOD_ALT | 0x100))  # device bits are ignored
        self.assertFalse(talk.matches(8, MOD_CTRL | MOD_ALT | MOD_CMD))
        self.assertEqual(parse_hotkey("Cmd + Shift + Space").mods, MOD_CMD | MOD_SHIFT)
        self.assertEqual(parse_hotkey("Cmd + Shift + Space").pretty(), "⇧⌘Space")
        self.assertEqual(parse_hotkey("option+command+j").keycode, 38)
        f5 = parse_hotkey("f5")
        self.assertEqual((f5.keycode, f5.pretty()), (96, "F5"))
        self.assertTrue(f5.matches(96, MOD_FN))  # MacBook function keys arrive with the fn flag

    def test_rejects_keys_you_type(self):
        for spec in ("c", "shift+c", "space", "ctrl+alt+nope", "hyper+c", ""):
            with self.assertRaises(HotkeyError, msg=spec):
                parse_hotkey(spec)

    def test_hold_to_talk_keys(self):
        self.assertEqual(parse_ptt("right_option"), "right_option")
        self.assertEqual(parse_ptt("Right Alt"), "right_option")
        self.assertIsNone(parse_ptt(""))
        with self.assertRaises(HotkeyError):
            parse_ptt("left_pinky")


class HotkeyListenerTests(unittest.TestCase):
    def start(self, quartz=None, ptt="right_option"):
        self.quartz, self.helper = quartz or FakeQuartz(), FakeAppHelper()
        patcher = mock.patch.dict(sys.modules, frameworks(self.quartz, self.helper))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.events = []
        self.manager = HotkeyManager(
            {"talk": parse_hotkey("ctrl+alt+c"), "type": parse_hotkey("ctrl+alt+j")},
            on_hotkey=self.events.append, on_escape=lambda: self.events.append("escape"), ptt=ptt,
            on_ptt_down=lambda: self.events.append("ptt down"), on_ptt_up=lambda: self.events.append("ptt up"))
        return self.manager.start()

    def send(self, etype, event):
        return self.manager._callback(None, etype, event, None)

    def test_hotkey_fires_once_and_never_reaches_the_app(self):
        self.assertTrue(self.start())
        combo = key(8, MOD_CTRL | MOD_ALT)
        self.assertIsNone(self.send(hk.KEY_DOWN, combo))
        self.assertIsNone(self.send(hk.KEY_DOWN, key(8, MOD_CTRL | MOD_ALT, repeat=True)))  # held down
        self.assertIsNone(self.send(hk.KEY_UP, combo))
        self.assertEqual(self.events, ["talk"])

    def test_everything_else_passes_through(self):
        self.start()
        for event in (key(8), key(8, MOD_CMD), key(38, MOD_CTRL)):
            self.assertIs(self.send(hk.KEY_DOWN, event), event)
            self.assertIs(self.send(hk.KEY_UP, event), event)
        self.assertEqual(self.events, [])

    def test_escape_is_noticed_but_still_reaches_the_app(self):
        self.start()
        esc = key(hk.ESCAPE)
        self.assertIs(self.send(hk.KEY_DOWN, esc), esc)
        self.assertEqual(self.events, ["escape"])

    def test_listen_only_mode_never_blocks_keys(self):
        self.assertTrue(self.start(FakeQuartz(allow_tap=False)))
        self.assertTrue(self.manager.listen_only)
        combo = key(38, MOD_CTRL | MOD_ALT)
        self.assertIs(self.send(hk.KEY_DOWN, combo), combo)
        self.assertEqual(self.events, ["type"])

    def test_without_permission_it_reports_failure(self):
        self.assertFalse(self.start(FakeQuartz(allow_tap=False, allow_listen=False)))

    def test_a_disabled_tap_is_switched_back_on(self):
        self.start()
        self.send(0xFFFFFFFE, key(0))
        self.assertEqual(self.quartz.enabled[-1], True)

    def test_hold_to_talk(self):
        self.start()
        self.send(hk.FLAGS_CHANGED, key(61, RIGHT_OPTION_DOWN))
        delay, fire, args = self.helper.later[0]
        self.assertEqual(delay, 0.3)  # only after a short hold
        fire(*args)
        self.send(hk.FLAGS_CHANGED, key(61, 0))
        self.assertEqual(self.events, ["ptt down", "ptt up"])

    def test_option_shortcuts_do_not_trigger_hold_to_talk(self):
        self.start()
        self.send(hk.FLAGS_CHANGED, key(61, RIGHT_OPTION_DOWN))
        self.send(hk.KEY_DOWN, key(14, RIGHT_OPTION_DOWN))  # ⌥E, typing an accent
        delay, fire, args = self.helper.later[0]
        fire(*args)
        self.send(hk.FLAGS_CHANGED, key(61, 0))
        self.assertEqual(self.events, [])


class MenuPanelTests(unittest.TestCase):
    def setUp(self):
        self.lines, self.notes = [], []
        self.panel = MenuPanel(self.lines.append, lambda title, message: self.notes.append((title, message)),
                               idle_hint="Press ⌃⌥C and speak.")

    def test_states_show_in_the_menu(self):
        self.panel.set_state("listening", None, "Speak now.")
        self.panel.set_state("thinking")
        self.panel.set_state("idle")
        self.assertEqual(self.lines, ["Listening. Speak now.", "Thinking…", "Ready. Press ⌃⌥C and speak."])
        self.assertEqual(self.notes, [])

    def test_errors_and_hints_become_notifications(self):
        self.panel.set_state("error", "Add your API key", "Put it in the .env file.")
        self.panel.set_state("error", "Add your API key", "Put it in the .env file.")  # not shown twice in a row
        self.panel.set_subtitle("Press Esc again to stop.")
        self.panel.show()
        self.assertEqual(self.notes, [("Add your API key", "Put it in the .env file."),
                                      ("Jarvis", "Press Esc again to stop.")])

    def test_reply_is_kept_and_shown_when_speech_is_off(self):
        self.panel.append("Hello ", "response")
        self.panel.append("there.", "response")
        self.assertEqual(self.panel.reply, "Hello there.")
        self.panel.reply_done("Hello there.")
        self.assertEqual(self.notes, [])  # spoken aloud, so no notification
        self.panel.notify_replies = True
        self.panel.reply_done("Hello there.")
        self.assertEqual(self.notes, [("Jarvis", "Hello there.")])
        self.panel.clear_text()
        self.assertTrue(self.panel.text_is_empty())


class MenuBarAppTests(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for patcher in (mock.patch.dict(os.environ), mock.patch.object(config_module, "ENV_PATH", NOWHERE)):
            patcher.start()
            self.addCleanup(patcher.stop)
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-key-for-unit-tests"
        self.notes = []

    def build(self, quartz=None, **cfg_overrides):
        overrides = {"paths": {"workspace": str(Path(self.tmp) / "ws")}, "voice": {"sounds": False}}
        for section, values in cfg_overrides.items():
            overrides.setdefault(section, {}).update(values)
        cfg = load_config(NOWHERE, overrides=overrides, data_dir=Path(self.tmp) / "data", local_path=NOWHERE)
        self.quartz, self.helper = quartz or FakeQuartz(), FakeAppHelper()
        patcher = mock.patch.dict(sys.modules, frameworks(self.quartz, self.helper))
        patcher.start()
        self.addCleanup(patcher.stop)
        sys.modules.pop("jarvis.ui.app", None)
        self.module = importlib.import_module("jarvis.ui.app")
        for name, value in (("notify", lambda title, message: self.notes.append((title, message))),
                            ("accessibility_trusted", mock.Mock(return_value=True))):
            p = mock.patch.object(self.module.mac, name, value)
            p.start()
            self.addCleanup(p.stop)
        app = self.module.JarvisApp(cfg)
        app.notify = lambda title, message: self.notes.append((title, message))  # no background threads in tests
        app.build(preload=False)
        self.addCleanup(app.assistant.shutdown)
        return app

    def test_menu_bar_icon_menu_and_hotkeys(self):
        app = self.build()
        titles = [item.title for item in app.status_item.menu.items]
        self.assertEqual(titles[2:], ["Talk", "Type a request…", "Stop", "-", "Copy last reply", "New conversation",
                                      "-", "Open workspace folder", "Edit settings", "Open log", "-", "Quit Jarvis"])
        self.assertTrue(titles[0].startswith("Ready."))
        talk = app.status_item.menu.items[2]
        self.assertEqual((talk.key, talk.mods), ("c", MOD_CTRL | MOD_ALT))
        self.assertEqual(app.hotkeys.tap, "tap")
        self.assertEqual(app.nsapp.policy, 1)  # no Dock icon
        self.assertEqual(app.status_item.button().image.name, "waveform.circle")
        self.assertIn(("Jarvis", "Jarvis is ready. Press ⌃⌥C or hold right ⌥ Option and speak. ⌃⌥J to type instead."),
                      self.notes)

    def test_states_reach_the_icon_and_the_status_line(self):
        app = self.build()
        app.ui.set_state("listening", None, "Speak now. I'll stop when you pause.")
        self.assertEqual(app.status_item.button().image.name, "mic.circle.fill")
        self.assertEqual(app.status_line_item.title, "Listening. Speak now. I'll stop when you pause.")
        self.assertIn("Listening", app.status_item.button().tooltip)
        app.ui.set_state("thinking", None, "“open safari”")
        self.assertEqual(app.status_item.button().image.name, "sparkles")

    def test_hotkeys_and_menu_clicks_reach_the_assistant(self):
        app = self.build()
        app.assistant.shutdown()
        app.assistant = mock.Mock(state="idle")
        app.on_hotkey("talk")
        app.assistant.on_talk.assert_called_once_with(tutor=False)
        app.on_hotkey("type")
        app.assistant.on_type.assert_called_once()
        app.menu_target.talk_(None)  # a click on "Talk" in the menu
        self.assertEqual(app.assistant.on_talk.call_count, 2)
        app.menu_stop()
        app.assistant.on_cancel.assert_called_once()
        app.menu_new_conversation()
        app.assistant.new_conversation.assert_called_once()
        app.last_response = "Hello there."
        with mock.patch.object(self.module.mac, "set_clipboard") as copy:
            app.menu_copy_last()
        copy.assert_called_once_with("Hello there.")
        app.menu_quit()
        app.assistant.shutdown.assert_called()
        self.assertTrue(self.helper.stopped)

    def test_without_accessibility_it_asks_once_and_keeps_trying(self):
        quartz = FakeQuartz(allow_tap=False, allow_listen=False)
        app = self.build(quartz=quartz)
        self.module.mac.accessibility_trusted.assert_called_with(prompt=True)
        self.assertTrue(any(title == "Hotkeys need Accessibility permission" for title, _ in self.notes))
        delay, retry, args = self.helper.later[0]
        self.assertEqual(delay, 3.0)
        quartz.allow_tap = True  # the user turned Accessibility on
        retry(*args)
        self.assertEqual(app.hotkeys.tap, "tap")
        self.assertIn(("Jarvis", "Hotkeys are working now."), self.notes)

    def test_missing_api_key_and_bad_settings_are_explained(self):
        os.environ["ANTHROPIC_API_KEY"] = ""
        self.build()
        self.assertTrue(any(title == "Add your API key" for title, _ in self.notes))
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test"
        self.notes.clear()
        app = self.build(hotkeys={"talk": "c"})
        self.assertEqual(app.bindings["talk"].pretty(), "⌃⌥C")  # fell back to the default
        self.assertTrue(any("would fire every time you type it" in message for _, message in self.notes))

    def test_replies_become_notifications_when_speech_is_off(self):
        app = self.build(voice={"tts": False})
        app.ui.show_response("Hello there.")
        self.assertIn(("Jarvis", "Hello there."), self.notes)
        self.assertEqual(app.last_response, "Hello there.")

    def test_dialogs_for_confirmations_and_typed_requests(self):
        app = self.build()
        with mock.patch.object(self.module.mac, "confirm_dialog", return_value=True) as confirm:
            self.assertTrue(app.ui.confirm("Jarvis wants to run a command", "rm -rf build"))
        confirm.assert_called_once()
        self.assertEqual(app.assistant.state, "idle")
        with mock.patch.object(self.module.mac, "ask_text_dialog", return_value="what's the time"):
            self.assertEqual(app.ui.ask_text("What should I do?"), "what's the time")


class DoctorHotkeyTests(unittest.TestCase):
    def test_hotkey_settings_are_checked(self):
        with mock.patch.object(config_module, "ENV_PATH", NOWHERE):
            good = load_config(NOWHERE, local_path=NOWHERE)
            bad = load_config(NOWHERE, local_path=NOWHERE, overrides={"hotkeys": {"talk": "c", "push_to_talk": "pinky"}})
        out = io.StringIO()
        r = Report(out, color=False)
        check_hotkeys(r, good)
        self.assertEqual([s for s, _, _ in r.items], [OK, OK, OK])
        self.assertIn("Talk: ⌃⌥C", out.getvalue())
        self.assertIn("Hold to talk: right ⌥ Option", out.getvalue())
        r = Report(io.StringIO(), color=False)
        check_hotkeys(r, bad)
        self.assertEqual([s for s, _, _ in r.items], [WARN, OK, WARN])


if __name__ == "__main__":
    unittest.main()
