"""Phase 8 tests: the floating panel.

The orb's shapes, the text layout and the placement are plain Python and tested directly. The AppKit
side runs against stand-ins for the AppKit classes it uses, which catches mistakes in its Python
logic without a Mac. Run:  python -m unittest discover -s tests -v
"""
from __future__ import annotations

import importlib
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

from test_app import FakeAppHelper, FakeQuartz, frameworks  # noqa: E402
from test_brain import NOWHERE  # noqa: E402

from jarvis import config as config_module  # noqa: E402
from jarvis.config import load_config  # noqa: E402
from jarvis.ui.hud_logic import (HEIGHT, WIDTH, orb_shapes, panel_origin, smooth_level, text_to_append,  # noqa: E402
                                 title_for)
from jarvis.ui.menu_panel import MenuPanel, PanelGroup  # noqa: E402

STATES = ("idle", "listening", "transcribing", "thinking", "speaking", "waiting", "error")


class OrbAndLayoutTests(unittest.TestCase):
    def test_every_state_fits_inside_the_orb_and_ends_with_the_core(self):
        for state in STATES:
            for t in (0.0, 0.7, 3.3):
                shapes = orb_shapes(state, t, 0.6, 52.0)
                self.assertEqual(shapes[-1][0], "core", state)
                for shape in shapes:
                    self.assertTrue(0 < shape[1] <= 25.0, (state, shape))
                    if shape[0] != "core":
                        alpha = shape[2] if shape[0] != "arc" else shape[4]
                        self.assertTrue(0.0 <= alpha <= 1.0, (state, shape))

    def test_listening_halo_follows_your_voice(self):
        quiet = orb_shapes("listening", 0, 0.0, 52.0)
        loud = orb_shapes("listening", 0, 1.0, 52.0)
        self.assertGreater(loud[0][1], quiet[0][1])
        self.assertGreater(loud[-1][1], quiet[-1][1])  # the core swells a little too

    def test_thinking_arcs_move_and_speaking_ripples_fade(self):
        self.assertNotEqual(orb_shapes("thinking", 0.0, 0, 52.0)[0][2], orb_shapes("thinking", 0.5, 0, 52.0)[0][2])
        rings = orb_shapes("speaking", 0.2, 0, 52.0)[:-1]
        by_size = sorted(rings, key=lambda ring: ring[1])
        self.assertGreater(by_size[0][2], by_size[-1][2])  # smaller rings are brighter

    def test_text_layout(self):
        transcript, last = "", ""
        for piece, kind in (("Let me ", "response"), ("check.", "response"), ("Running: ls", "status"),
                            ("Found 3 files.", "response"), ("Oops", "error")):
            transcript += text_to_append(transcript, piece, kind, last)
            last = kind
        self.assertEqual(transcript, "Let me check.\nRunning: ls\nFound 3 files.\nOops\n")

    def test_titles_level_and_placement(self):
        self.assertEqual(title_for("thinking"), "Working on it")
        self.assertEqual(title_for("thinking", "Custom"), "Custom")
        level = 0.0
        for _ in range(20):
            level = smooth_level(level, 1.0, 1 / 30)
        self.assertGreater(level, 0.9)
        screen = (0.0, 0.0, 1440.0, 875.0)
        self.assertEqual(panel_origin(screen, "top-right"), (1440 - WIDTH - 14, 875 - HEIGHT - 14))
        self.assertEqual(panel_origin(screen, "bottom-left"), (14, 14))
        self.assertEqual(panel_origin(screen, "top-center")[0], (1440 - WIDTH) / 2)


class PanelGroupTests(unittest.TestCase):
    def test_updates_reach_every_panel(self):
        first, second = mock.Mock(), MenuPanel(lambda line: None, lambda title, message: None)
        first.text_is_empty.return_value = False
        group = PanelGroup(first, None, second)
        group.set_state("thinking", None, "“hello”")
        first.set_state.assert_called_once_with("thinking", None, "“hello”")
        self.assertEqual(second.status, "Thinking. “hello”")
        self.assertFalse(group.text_is_empty())  # the first panel answers
        group.reply_done("hi")  # only the menu panel has this
        with self.assertRaises(AttributeError):
            PanelGroup(object()).nothing_has_this()


# ---------------------------------------------------------------- stand-ins for AppKit's panel classes

class Point:
    def __init__(self, x, y):
        self.x, self.y = x, y


class Size:
    def __init__(self, width, height):
        self.width, self.height = width, height


class Rect:
    def __init__(self, x, y, w, h):
        self.origin, self.size = Point(x, y), Size(w, h)


class Settable:
    """Remembers setter calls, like setMaterial_(13), so tests can inspect them."""

    @classmethod
    def alloc(cls):
        obj = cls.__new__(cls)
        obj.props = {}
        return obj

    def init(self):
        return self

    def initWithFrame_(self, frame):
        self.frame_rect, self.subviews = frame, []
        return self

    def addSubview_(self, view):
        self.subviews.append(view)

    def __getattr__(self, name):
        if name.startswith("set"):
            return lambda *args: self.props.__setitem__(name, args[0] if len(args) == 1 else args)
        raise AttributeError(name)


class FakeView(Settable):
    def bounds(self):
        return Rect(0, 0, self.frame_rect.size.width, self.frame_rect.size.height)

    def setNeedsDisplay_(self, flag):
        self.props["redraws"] = self.props.get("redraws", 0) + 1


class FakeEffectView(FakeView):
    def layer(self):
        return mock.Mock()


class FakeTextField(Settable):
    def setStringValue_(self, value):
        self.value = value

    def cell(self):
        return mock.Mock()


class FakeStorage:
    def __init__(self):
        self.text = ""

    def string(self):
        return self.text

    def length(self):
        return len(self.text)

    def appendAttributedString_(self, attributed):
        self.text += attributed.text


class FakeTextView(Settable):
    def initWithFrame_(self, frame):
        super().initWithFrame_(frame)
        self.storage = FakeStorage()
        return self

    def setString_(self, value):
        self.storage.text = value

    def textStorage(self):
        return self.storage

    def textContainer(self):
        return mock.Mock()

    def scrollRangeToVisible_(self, rng):
        self.scrolled_to = rng


class FakeScrollView(Settable):
    def contentSize(self):
        return Size(self.frame_rect.size.width, self.frame_rect.size.height)

    def setDocumentView_(self, view):
        self.document = view


class FakeAttributed:
    @classmethod
    def alloc(cls):
        return cls()

    def initWithString_attributes_(self, text, attributes):
        self.text = text
        return self


class FakePanel(Settable):
    def initWithContentRect_styleMask_backing_defer_(self, rect, style, backing, defer):
        self.rect, self.style, self.visible, self.alpha = rect, style, False, 1.0
        return self

    def setFrame_display_(self, rect, display):
        self.rect = rect

    def frame(self):
        return self.rect

    def setAlphaValue_(self, alpha):
        self.alpha = alpha

    def orderFrontRegardless(self):
        self.visible = True

    def orderOut_(self, sender):
        self.visible = False


class FakeTimer:
    def __init__(self, interval, target, selector):
        self.interval, self.target, self.selector, self.valid = interval, target, selector, True

    def invalidate(self):
        self.valid = False


def panel_frameworks(quartz, helper, mouse):
    modules = frameworks(quartz, helper)
    appkit, foundation = modules["AppKit"], modules["Foundation"]
    screen = mock.Mock()
    screen.visibleFrame.return_value = Rect(0, 0, 1440, 875)
    timers = []

    class NSTimer:
        @staticmethod
        def timerWithTimeInterval_target_selector_userInfo_repeats_(interval, target, selector, info, repeats):
            timers.append(FakeTimer(interval, target, selector))
            return timers[-1]

    names = dict(
        NSAppearance=mock.Mock(), NSAttributedString=FakeAttributed, NSBezierPath=mock.Mock(), NSColor=mock.Mock(),
        NSEvent=types.SimpleNamespace(mouseLocation=lambda: mouse[0]), NSFont=mock.Mock(),
        NSFontAttributeName="font", NSForegroundColorAttributeName="color", NSGradient=mock.Mock(),
        NSMutableParagraphStyle=mock.Mock(), NSPanel=FakePanel, NSParagraphStyleAttributeName="paragraph",
        NSScreen=types.SimpleNamespace(screens=lambda: [screen], mainScreen=lambda: screen),
        NSScrollView=FakeScrollView, NSTextField=FakeTextField, NSTextView=FakeTextView, NSView=FakeView,
        NSVisualEffectView=FakeEffectView)
    for name, value in names.items():
        setattr(appkit, name, value)
    foundation.NSMakePoint, foundation.NSMakeSize, foundation.NSMakeRect = Point, Size, Rect
    foundation.NSPointInRect = lambda p, r: (r.origin.x <= p.x <= r.origin.x + r.size.width
                                             and r.origin.y <= p.y <= r.origin.y + r.size.height)
    foundation.NSRunLoop = types.SimpleNamespace(mainRunLoop=lambda: mock.Mock())
    foundation.NSTimer = NSTimer
    objc = types.ModuleType("objc")
    objc.super = super
    modules["objc"] = objc
    return modules, timers


class PanelOnFakeAppKitTests(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for patcher in (mock.patch.dict(os.environ), mock.patch.object(config_module, "ENV_PATH", NOWHERE)):
            patcher.start()
            self.addCleanup(patcher.stop)
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-key-for-unit-tests"
        self.mouse = [Point(0, 0)]
        self.helper = FakeAppHelper()
        modules, self.timers = panel_frameworks(FakeQuartz(), self.helper, self.mouse)
        patcher = mock.patch.dict(sys.modules, modules)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("jarvis.ui.hud", "jarvis.ui.app"):
            sys.modules.pop(name, None)

    def cfg(self, **ui):
        overrides = {"paths": {"workspace": str(Path(self.tmp) / "ws")}, "voice": {"sounds": False}, "ui": ui}
        return load_config(NOWHERE, overrides=overrides, data_dir=Path(self.tmp) / "data", local_path=NOWHERE)

    def test_panel_states_text_and_animation(self):
        hud = importlib.import_module("jarvis.ui.hud").Hud(self.cfg(), idle_hint="Press ⌃⌥C and speak.")
        self.assertEqual((hud.title.value, hud.subtitle.value), ("Ready", "Press ⌃⌥C and speak."))
        self.assertFalse(hud.panel.visible)
        hud.set_state("listening", None, "Speak now.")
        self.assertEqual((hud.title.value, hud.subtitle.value), ("Listening", "Speak now."))
        self.assertTrue(hud.panel.visible)
        self.assertEqual(self.timers[-1].selector, "fire:")  # 30 fps animation while it's showing
        hud.set_level(0.9)
        before = hud.orb.jsmooth
        hud.last_tick -= 0.05
        hud.tick()
        self.assertGreater(hud.orb.jsmooth, before)
        for state in ("idle", "listening", "thinking", "speaking", "error"):
            hud.orb.jstate = state
            hud.orb.drawRect_(None)  # the drawing code runs for every state
        hud.append("Let me check.", "response")
        hud.append("Running: ls", "status")
        hud.append("Found 3 files.", "response")
        self.assertEqual(hud.textview.storage.text, "Let me check.\nRunning: ls\nFound 3 files.")
        self.assertFalse(hud.text_is_empty())
        hud.clear_text()
        self.assertTrue(hud.text_is_empty())

    def test_hiding_waits_for_your_mouse_and_screenshots_skip_the_panel(self):
        hud = importlib.import_module("jarvis.ui.hud").Hud(self.cfg(hud_position="bottom-left"))
        self.assertEqual((hud.panel.rect.origin.x, hud.panel.rect.origin.y), (14, 14))
        hud.set_state("speaking")
        hud.set_state("idle")
        self.mouse[0] = Point(20, 20)  # hovering over the panel
        hud.schedule_hide(5)
        delay, autohide, args = self.helper.later[-1]
        autohide(*args)
        self.assertTrue(hud.panel.visible)
        self.assertEqual(self.helper.later[-1][0], 3.0)  # checks again shortly
        self.mouse[0] = Point(900, 600)
        delay, autohide, args = self.helper.later[-1]
        autohide(*args)
        self.assertFalse(hud.panel.visible)
        self.assertFalse(self.timers[-1].valid)  # animation stops while hidden
        hud.show()
        hud.set_capture_hidden(True)
        self.assertEqual(hud.panel.alpha, 0.0)
        hud.set_capture_hidden(False)
        self.assertEqual(hud.panel.alpha, 1.0)

    def test_app_uses_the_panel_and_keeps_the_menu_status_line(self):
        module = importlib.import_module("jarvis.ui.app")
        notes = []
        with mock.patch.object(module.mac, "accessibility_trusted", return_value=True), \
                mock.patch.object(module.mac, "notify", lambda title, message: notes.append((title, message))):
            app = module.JarvisApp(self.cfg())
            app.notify = lambda title, message: notes.append((title, message))
            app.build(preload=False)
            self.addCleanup(app.assistant.shutdown)
            self.assertTrue(app.has_panel)
            panel = app.hud.panels[0]
            self.assertEqual(panel.title.value, "Jarvis is ready")
            self.assertTrue(panel.panel.visible)
            app.ui.set_state("error", "Add your API key", "Put it in the .env file.")
            self.assertEqual(panel.title.value, "Add your API key")
            self.assertEqual(app.status_line_item.title, "Add your API key: Put it in the .env file.")
            self.assertEqual(notes, [])  # the panel shows it, so no notifications
            panel.hide()
            app.menu_show_panel()
            self.assertTrue(panel.panel.visible)


if __name__ == "__main__":
    unittest.main()
