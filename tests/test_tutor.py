"""Phase 9 tests: tutor mode (screenshots, the pointer and the walkthrough flow).

The pointer's geometry is plain Python and tested directly; the AppKit overlay runs against stand-ins.
Screens are simulated: nothing is captured and no window appears.  Run:  python -m unittest discover -s tests -v
"""
from __future__ import annotations

import contextlib
import importlib
import io
import json
import logging
import os
import shutil
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_app import FakeAppHelper, FakeQuartz, frameworks  # noqa: E402
from test_assistant import FakeSpeaker  # noqa: E402
from test_brain import NOWHERE, FakeAPI, reply, text, tool  # noqa: E402
from test_panel import FakeAttributed, FakePanel, Point, Rect, panel_frameworks  # noqa: E402

from jarvis import cli, mac  # noqa: E402
from jarvis import config as config_module  # noqa: E402
from jarvis.assistant import Assistant  # noqa: E402
from jarvis.brain import Brain  # noqa: E402
from jarvis.config import load_config  # noqa: E402
from jarvis.doctor import WARN, Report, check_mac_permissions  # noqa: E402
from jarvis.memory import Memory  # noqa: E402
from jarvis.tools import Cancelled, ToolContext, build_registry, screen_tools  # noqa: E402
from jarvis.ui import ConsoleUI  # noqa: E402
from jarvis.ui.menu_panel import NullOverlay  # noqa: E402
from jarvis.ui.overlay_logic import arrow_points, bubble_layout, from_mouse, glide, glow_rings, pointer_area  # noqa: E402

SCREEN_W, SCREEN_H = 1440.0, 900.0


def make_shot():
    return mac.Shot(jpeg=b"\xff\xd8fake-jpeg", width=1280, height=800, screen_w=SCREEN_W, screen_h=SCREEN_H)


class Base(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for patcher in (mock.patch.dict(os.environ), mock.patch.object(config_module, "ENV_PATH", NOWHERE),
                        mock.patch.object(screen_tools, "GLIDE_SECONDS", 0.0)):
            patcher.start()
            self.addCleanup(patcher.stop)
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-key-for-unit-tests"
        overrides = {"paths": {"workspace": str(Path(self.tmp) / "ws")}, "voice": {"sounds": False}}
        self.cfg = load_config(NOWHERE, overrides=overrides, data_dir=Path(self.tmp) / "data", local_path=NOWHERE)
        self.out = io.StringIO()
        self.ui = ConsoleUI(stream=self.out, interactive=False, color=False)


class PointerGeometryTests(unittest.TestCase):
    def test_bubble_sits_below_right_and_flips_near_edges(self):
        bubble, label = bubble_layout(100, 100, 120, 18, SCREEN_W, SCREEN_H)
        self.assertEqual(bubble[:2], (126, 122))  # below-right of the tip
        self.assertEqual(label[:2], (139, 130))
        right, _ = bubble_layout(1400, 100, 120, 18, SCREEN_W, SCREEN_H)
        self.assertLess(right[0] + right[2], 1400)  # flipped to the left of the tip
        bottom, _ = bubble_layout(100, 890, 120, 18, SCREEN_W, SCREEN_H)
        self.assertLess(bottom[1] + bottom[3], 890)  # flipped above the tip
        for x, y in ((0, 0), (SCREEN_W, SCREEN_H), (5, SCREEN_H - 5)):
            b, _ = bubble_layout(x, y, 300, 60, SCREEN_W, SCREEN_H)
            self.assertTrue(b[0] >= 8 and b[1] >= 8 and b[0] + b[2] <= SCREEN_W and b[1] + b[3] <= SCREEN_H, (x, y))

    def test_arrow_glow_and_glide(self):
        self.assertEqual(arrow_points(10, 20)[0], (10, 20))  # the tip is exactly on the target
        for pulse in (0.0, 0.4, 1.3):
            self.assertTrue(all(0 < radius <= 36 and 0 < alpha < 1 for radius, alpha in glow_rings(pulse)))
        self.assertEqual(glide((0, 0), (100, 50), 0.0), ((0.0, 0.0), False))
        (mid_x, _), arrived = glide((0, 0), (100, 50), 0.275)
        self.assertAlmostEqual(mid_x, 50.0)
        self.assertFalse(arrived)
        self.assertEqual(glide((0, 0), (100, 50), 5.0), ((100.0, 50.0), True))
        self.assertEqual(from_mouse(100, 800, SCREEN_H), (100.0, 100.0))  # macOS counts from the bottom
        bubble, _ = bubble_layout(100, 100, 120, 18, SCREEN_W, SCREEN_H)
        area = pointer_area(100, 100, bubble)
        self.assertTrue(area[0] + area[2] >= bubble[0] + bubble[2])  # redraws cover the bubble too


class ScreenToolTests(Base):
    def setUp(self):
        super().setUp()
        self.ctx = ToolContext(cfg=self.cfg, ui=self.ui)
        self.reg = build_registry(self.cfg)

    def test_screenshot_needs_macos(self):
        with mock.patch.object(screen_tools.sys, "platform", "linux"):
            result = self.reg.execute("take_screenshot", {}, self.ctx)
        self.assertTrue(result.is_error)
        self.assertIn("macOS", result.text)

    def test_screenshot_hides_jarvis_and_warns_without_permission(self):
        hidden = []

        @contextlib.contextmanager
        def hidden_for_capture():
            hidden.append(True)
            yield

        self.ui.hidden_for_capture = hidden_for_capture
        with mock.patch.object(screen_tools.sys, "platform", "darwin"), \
                mock.patch.object(mac, "capture_screen", return_value=make_shot()), \
                mock.patch.object(mac, "screen_recording_allowed", return_value=False) as allowed:
            result = self.reg.execute("take_screenshot", {}, self.ctx)
        self.assertEqual(hidden, [True])  # the panel and pointer stay out of the picture
        self.assertEqual(result.to_content()[0]["type"], "image")
        self.assertIn("1280x800", result.text)
        self.assertIn("Screen Recording permission is off", result.text)
        allowed.assert_called_with(request=True)
        self.assertEqual(self.ctx.shot.width, 1280)

    def test_point_at_maps_screenshot_pixels_to_the_screen(self):
        self.assertTrue(self.reg.execute("point_at", {"x": 1, "y": 1, "label": "Here"}, self.ctx).is_error)
        self.ctx.shot = make_shot()
        said = []
        self.ctx.speak = said.append
        result = self.reg.execute("point_at", {"x": 640, "y": 400, "label": "Click Run"}, self.ctx)
        self.assertEqual(result.text, "Pointing at (640, 400): Click Run.")
        self.assertEqual(self.ui.pointer, (720.0, 450.0, "Click Run"))  # 1280 px wide shot of a 1440 pt screen
        self.assertEqual(said, ["Click Run"])
        self.reg.execute("point_at", {"x": 5000, "y": -20, "label": "Corner"}, self.ctx)
        self.assertEqual(self.ui.pointer[:2], (1279 * SCREEN_W / 1280, 0.0))  # clamped onto the screen
        self.reg.execute("hide_pointer", {}, self.ctx)
        self.assertIsNone(self.ui.pointer)

    def test_labels_stay_up_long_enough_when_speech_is_off(self):
        self.ctx.shot = make_shot()
        with mock.patch.object(screen_tools, "reading_time", return_value=0.3):
            started = time.monotonic()
            self.reg.execute("point_at", {"x": 10, "y": 10, "label": "Read me"}, self.ctx)
            self.assertGreaterEqual(time.monotonic() - started, 0.3)
            self.ctx.cancel.set()
            with self.assertRaises(Cancelled):
                self.reg.execute("point_at", {"x": 10, "y": 10, "label": "Stopped"}, self.ctx)


class TutorFlowTests(Base):
    def make(self, responses):
        self.api = FakeAPI(responses)
        memory = Memory(Path(self.tmp) / "memory.json")
        registry = build_registry(self.cfg)
        self.speaker = FakeSpeaker()
        return Assistant(self.cfg, self.ui, brain=Brain(self.cfg, registry, memory, client=self.api.client()),
                         memory=memory, registry=registry, speaker=self.speaker)

    def test_walkthrough_sees_points_and_explains(self):
        a = self.make([reply([tool("toolu_p", "point_at", {"x": 640, "y": 400, "label": "Click Run"})], "tool_use"),
                       reply([text("That's the Run button.")])])
        with mock.patch.object(Assistant, "_capture_for_tutor", return_value=make_shot()):
            self.assertEqual(a.process_text("where is the run button?", tutor=True), "That's the Run button.")
        first = self.api.requests[0]["messages"][-1]["content"]
        self.assertEqual(first[0]["type"], "image")  # Claude sees the screen first
        self.assertIn("Mode: tutor", first[1]["text"])
        self.assertIn("1280x800", first[1]["text"])
        self.assertEqual(self.ui.pointer, (720.0, 450.0, "Click Run"))
        self.assertEqual(self.speaker.said, ["Click Run.", "That's the Run button."])  # spoken labels get a full stop
        self.assertNotIn('"image"', json.dumps(a.brain.history, default=str))  # screenshots aren't kept

    def test_tutor_in_the_terminal_chat(self):
        api = FakeAPI([reply([text("That's your editor.")])])
        memory = Memory(Path(self.tmp) / "m.json")
        chat = cli.Chat(self.cfg, ui=self.ui, memory=memory,
                        brain=Brain(self.cfg, build_registry(self.cfg), memory, client=api.client()))
        with mock.patch.object(Assistant, "_capture_for_tutor", return_value=make_shot()), \
                mock.patch("sys.stdout", io.StringIO()), \
                mock.patch("builtins.input", side_effect=["/tutor what's this window?", "/quit"]):
            self.assertEqual(cli.run_cli(self.cfg, chat=chat), 0)
        sent = api.requests[0]["messages"][-1]["content"]
        self.assertEqual(sent[0]["type"], "image")
        self.assertTrue(sent[1]["text"].endswith("what's this window?"))


class MeasuredText(FakeAttributed):
    drawn: list = []

    def boundingRectWithSize_options_(self, size, options):
        return Rect(0, 0, 8.0 * len(self.text), 18.0)

    def drawWithRect_options_(self, rect, options):
        MeasuredText.drawn.append((self.text, rect.origin.x, rect.origin.y))


class FakeWindow(FakePanel):
    pass


class OverlayOnFakeAppKitTests(Base):
    def setUp(self):
        super().setUp()
        self.mouse = [Point(100, 800)]
        self.helper = FakeAppHelper()
        modules, self.timers = panel_frameworks(FakeQuartz(), self.helper, self.mouse)
        appkit = modules["AppKit"]
        screen = mock.Mock()
        screen.frame.return_value = Rect(0, 0, SCREEN_W, SCREEN_H)
        screen.visibleFrame.return_value = Rect(0, 0, SCREEN_W, 875)
        appkit.NSScreen = types.SimpleNamespace(screens=lambda: [screen], mainScreen=lambda: screen)
        appkit.NSWindow, appkit.NSAttributedString = FakeWindow, MeasuredText
        appkit.NSGraphicsContext, appkit.NSShadow = mock.Mock(), mock.Mock()
        appkit.NSRectFillUsingOperation = lambda rect, op: None
        patcher = mock.patch.dict(sys.modules, modules)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("jarvis.ui.hud", "jarvis.ui.overlay", "jarvis.ui.app"):
            sys.modules.pop(name, None)

    def test_pointer_glides_in_draws_and_hides(self):
        overlay = importlib.import_module("jarvis.ui.overlay").TutorOverlay()
        overlay.point_at(720, 450, "Click Run")
        self.assertTrue(overlay.window.visible)
        self.assertEqual(overlay.view.jpos, (100.0, 100.0))  # starts from the mouse
        self.assertAlmostEqual(self.timers[-1].interval, 1 / 60)
        overlay.anim_start -= 1.0  # time passes
        overlay.tick()
        self.assertEqual(overlay.view.jpos, (720.0, 450.0))
        MeasuredText.drawn.clear()
        overlay.view.drawRect_(Rect(0, 0, SCREEN_W, SCREEN_H))
        self.assertEqual(MeasuredText.drawn, [("Click Run", 759.0, 480.0)])  # label beside the arrow
        overlay.hide_after(5)
        delay, hide_if, args = self.helper.later[-1]
        overlay.point_at(300, 300, "Next step")  # a newer step cancels the pending hide
        hide_if(*args)
        self.assertTrue(overlay.window.visible)
        overlay.set_capture_hidden(True)
        self.assertEqual(overlay.window.alpha, 0.0)
        overlay.set_capture_hidden(False)
        overlay.hide()
        self.assertFalse(overlay.window.visible)
        self.assertFalse(self.timers[-1].valid)

    def test_app_uses_the_pointer_and_hides_it_for_screenshots(self):
        module = importlib.import_module("jarvis.ui.app")
        with mock.patch.object(module.mac, "accessibility_trusted", return_value=True):
            app = module.JarvisApp(self.cfg)
            app.notify = lambda title, message: None
            app.build(preload=False)
            self.addCleanup(app.assistant.shutdown)
        self.assertFalse(isinstance(app.overlay, NullOverlay))
        app.ui.point_at(720, 450, "Click Run")
        self.assertTrue(app.overlay.window.visible)
        with app.ui.hidden_for_capture():
            self.assertEqual(app.overlay.window.alpha, 0.0)
        self.assertEqual(app.overlay.window.alpha, 1.0)


class FallbackAndDoctorTests(Base):
    def test_without_appkit_tutor_mode_still_works_without_a_pointer(self):
        patcher = mock.patch.dict(sys.modules, frameworks(FakeQuartz(), FakeAppHelper()))
        patcher.start()
        self.addCleanup(patcher.stop)
        sys.modules.pop("jarvis.ui.app", None)
        module = importlib.import_module("jarvis.ui.app")
        with mock.patch.object(module.mac, "accessibility_trusted", return_value=True):
            app = module.JarvisApp(self.cfg)
            app.notify = lambda title, message: None
            app.build(preload=False)
            self.addCleanup(app.assistant.shutdown)
        self.assertIsInstance(app.overlay, NullOverlay)
        app.ui.point_at(1, 2, "fine")  # quietly does nothing

    def test_doctor_checks_screen_recording(self):
        out = io.StringIO()
        r = Report(out, color=False)
        with mock.patch.object(sys, "platform", "darwin"), \
                mock.patch.object(mac, "accessibility_trusted", return_value=True), \
                mock.patch.object(mac, "screen_recording_allowed", return_value=False):
            check_mac_permissions(r)
        self.assertIn((WARN, "Screen Recording: not allowed yet"), [(s, label) for s, label, _ in r.items])


if __name__ == "__main__":
    unittest.main()
