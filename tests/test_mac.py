"""Phase 4 tests: the Mac-control tools.

macOS is simulated with mocks, so these run on Linux (GitHub CI) and never pop up dialogs,
notifications or permission prompts on your Mac.  Run:  python -m unittest discover -s tests -v
"""
from __future__ import annotations

import io
import logging
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_brain import NOWHERE, FakeAPI, reply, text, tool  # noqa: E402

from jarvis import cli, mac  # noqa: E402
from jarvis import config as config_module  # noqa: E402
from jarvis.brain import Brain  # noqa: E402
from jarvis.config import load_config  # noqa: E402
from jarvis.memory import Memory  # noqa: E402
from jarvis.prompts import build_system_prompt  # noqa: E402
from jarvis.tools import Cancelled, ToolContext, build_registry, mac_tools  # noqa: E402
from jarvis.ui import ConsoleUI  # noqa: E402

OPEN_OK = mock.Mock(returncode=0, stdout="", stderr="")
TERMINAL = {"name": "Terminal", "pid": 111, "bundle_id": "com.apple.Terminal", "window": ""}


class Base(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for patcher in (mock.patch.dict(os.environ), mock.patch.object(config_module, "ENV_PATH", NOWHERE)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.cfg = load_config(NOWHERE, overrides={"paths": {"workspace": str(Path(self.tmp) / "ws")}},
                               data_dir=Path(self.tmp) / "data", local_path=NOWHERE)
        Path(self.cfg.paths.workspace).mkdir(parents=True)
        self.ui = ConsoleUI(stream=io.StringIO(), interactive=False, confirm_answer=False, color=False)
        self.ctx = ToolContext(cfg=self.cfg, ui=self.ui, memory=Memory(Path(self.tmp) / "mem.json"))
        self.reg = build_registry(self.cfg)

    def run_tool(self, name, **args):
        return self.reg.execute(name, args, self.ctx)


class AppleScriptQuotingTests(unittest.TestCase):
    def test_strings_are_escaped(self):
        self.assertEqual(mac.as_quote('say "hi"'), '"say \\"hi\\""')
        self.assertEqual(mac.as_quote("a\\b"), '"a\\\\b"')
        self.assertEqual(mac.as_quote("one\ntwo\tthree"), '"one\\ntwo\\tthree"')


class OpenTests(Base):
    def opened(self, **args):
        with mock.patch.object(mac_tools.subprocess, "run", return_value=OPEN_OK) as run:
            result = self.run_tool("open", **args)
        return result, (run.call_args[0][0] if run.called else None)

    def test_websites_files_and_apps(self):
        result, argv = self.opened(target="github.com")
        self.assertFalse(result.is_error, result.text)
        self.assertEqual(argv, ["open", "https://github.com"])
        self.assertEqual(self.opened(target="localhost:3000")[1], ["open", "http://localhost:3000"])
        self.assertEqual(self.opened(target="https://example.com/a?b=1", app="Safari")[1],
                         ["open", "-a", "Safari", "https://example.com/a?b=1"])
        self.assertEqual(self.opened(app="Calculator")[1], ["open", "-a", "Calculator"])
        notes = Path(self.cfg.paths.workspace, "notes.md")
        notes.write_text("x")
        self.assertEqual(self.opened(target="notes.md", app="TextEdit", background=True)[1],
                         ["open", "-g", "-a", "TextEdit", str(notes)])

    def test_missing_things_are_errors_not_websites(self):
        for target in ("missing-folder", "todo.md"):
            result, argv = self.opened(target=target)
            self.assertTrue(result.is_error, target)
            self.assertIsNone(argv)
            self.assertIn("Nothing exists", result.text)

    def test_open_failure_is_reported(self):
        failed = mock.Mock(returncode=1, stdout="", stderr="Unable to find application named 'Nope'")
        with mock.patch.object(mac_tools.subprocess, "run", return_value=failed):
            result = self.run_tool("open", app="Nope")
        self.assertTrue(result.is_error)
        self.assertIn("Unable to find application", result.text)


class AppleScriptTests(Base):
    DELETE = 'tell application "Finder" to delete file "a.txt"'

    def test_everyday_script_runs(self):
        with mock.patch.object(mac, "osascript", return_value="42") as osa:
            self.assertEqual(self.run_tool("run_applescript", script="set volume output volume 30").text, "42")
        osa.assert_called_once()

    def test_risky_script_needs_approval(self):
        with mock.patch.object(mac, "osascript") as osa:
            result = self.run_tool("run_applescript", script=self.DELETE)
        self.assertTrue(result.is_error)
        self.assertIn("declined", result.text)
        osa.assert_not_called()
        self.ui.confirm_answer = True
        with mock.patch.object(mac, "osascript", return_value=""):
            self.assertFalse(self.run_tool("run_applescript", script=self.DELETE).is_error)

    def test_permission_errors_explain_the_fix(self):
        denied = mac.AppleScriptError("execution error: Not authorized to send Apple events to Music. (-1743)")
        with mock.patch.object(mac, "osascript", side_effect=denied):
            result = self.run_tool("run_applescript", script='tell application "Music" to play')
        self.assertTrue(result.is_error)
        self.assertIn("Automation", result.text)

    def test_cancel(self):
        self.ctx.cancel.set()
        with mock.patch.object(mac, "osascript", side_effect=mac.AppleScriptError("cancelled")):
            with self.assertRaises(Cancelled):
                self.reg.execute("run_applescript", {"script": "delay 10"}, self.ctx)


class ClipboardAndTypingTests(Base):
    def test_clipboard(self):
        with mock.patch.object(mac, "set_clipboard") as set_clipboard:
            self.assertIn("Copied 5 characters", self.run_tool("clipboard", action="set", text="hello").text)
        set_clipboard.assert_called_once_with("hello")
        with mock.patch.object(mac, "get_clipboard", return_value="copied text"):
            self.assertEqual(self.run_tool("clipboard", action="get").text, "copied text")
        with mock.patch.object(mac, "get_clipboard", return_value=""):
            self.assertIn("empty", self.run_tool("clipboard", action="get").text)

    def test_typing_needs_accessibility(self):
        with mock.patch.object(mac, "accessibility_trusted", return_value=False), \
                mock.patch.object(mac, "paste_text") as paste:
            result = self.run_tool("type_text", text="hi")
        self.assertTrue(result.is_error)
        self.assertIn("Accessibility", result.text)
        paste.assert_not_called()

    def test_types_into_the_app_in_front(self):
        self.ctx.host_app = TERMINAL
        with mock.patch.object(mac, "accessibility_trusted", return_value=True), \
                mock.patch.object(mac, "frontmost_app", return_value={"name": "TextEdit", "pid": 222}), \
                mock.patch.object(mac, "paste_text") as paste:
            result = self.run_tool("type_text", text="Dear team,", press_enter=True)
        self.assertFalse(result.is_error, result.text)
        paste.assert_called_once_with("Dear team,", press_enter=True)
        self.assertIn("pressed Return", result.text)

    def test_never_types_into_the_terminal_running_jarvis(self):
        self.ctx.host_app = TERMINAL
        with mock.patch.object(mac, "accessibility_trusted", return_value=True), \
                mock.patch.object(mac, "frontmost_app", return_value=TERMINAL), \
                mock.patch.object(mac, "paste_text") as paste:
            result = self.run_tool("type_text", text="oops")
        self.assertTrue(result.is_error)
        self.assertIn("terminal running Jarvis", result.text)
        paste.assert_not_called()

    def test_goes_back_to_the_app_the_user_was_in(self):
        notes = {"name": "Notes", "pid": os.getpid() + 1}  # any pid other than this test process
        self.ctx.frontmost = notes
        with mock.patch.object(mac, "accessibility_trusted", return_value=True), \
                mock.patch.object(mac, "activate_app", return_value=True) as activate, \
                mock.patch.object(mac, "paste_text"):
            self.assertIn("into Notes", self.run_tool("type_text", text="x").text)
        activate.assert_called_once_with(notes)
        with mock.patch.object(mac, "accessibility_trusted", return_value=True), \
                mock.patch.object(mac, "activate_app", return_value=False):
            result = self.run_tool("type_text", text="x")
        self.assertTrue(result.is_error)
        self.assertIn("couldn't bring Notes back", result.text)

    def test_selected_text(self):
        with mock.patch.object(mac, "accessibility_trusted", return_value=True), \
                mock.patch.object(mac, "copy_selection", return_value="selected words"):
            self.assertEqual(self.run_tool("get_selected_text").text, "selected words")
        with mock.patch.object(mac, "accessibility_trusted", return_value=True), \
                mock.patch.object(mac, "copy_selection", return_value=""):
            self.assertIn("Nothing is selected", self.run_tool("get_selected_text").text)
        self.ctx.host_app = TERMINAL  # terminal mode: other apps' selections are out of reach
        result = self.run_tool("get_selected_text")
        self.assertTrue(result.is_error)
        self.assertIn("clipboard", result.text)

    def test_front_app_and_notifications(self):
        with mock.patch.object(mac, "frontmost_app", return_value={"name": "Safari", "pid": 5, "window": "GitHub"}):
            self.assertIn('Right now: Safari, window "GitHub"', self.run_tool("frontmost_app").text)
        with mock.patch.object(mac, "notify") as notify:
            self.run_tool("notify", message="Build finished")
        notify.assert_called_once_with("Jarvis", "Build finished")


class RegistryAndPromptTests(Base):
    def test_phase_four_tools_and_guidance(self):
        names = self.reg.names()
        for name in ("open", "run_applescript", "clipboard", "type_text", "get_selected_text", "frontmost_app", "notify"):
            self.assertIn(name, names)
        prompt = build_system_prompt(self.cfg, names, web_search=True)
        for phrase in ("run_applescript", "open tool", "never into the terminal", "clipboard tool"):
            self.assertIn(phrase, prompt)

    def test_helpers_degrade_gracefully_off_macos(self):
        if sys.platform == "darwin":
            self.skipTest("only meaningful off macOS")
        self.assertEqual(mac.frontmost_app()["pid"], 0)
        self.assertFalse(mac.accessibility_trusted())
        with self.assertRaises(mac.AppleScriptError):
            mac.osascript("beep")


class EndToEndTests(Base):
    def test_open_a_website_in_safari(self):
        api = FakeAPI([reply([tool("toolu_o", "open", {"target": "github.com", "app": "Safari"})], "tool_use"),
                       reply([text("GitHub is open in Safari.")])])
        brain = Brain(self.cfg, self.reg, self.ctx.memory, client=api.client())
        with mock.patch.object(mac_tools.subprocess, "run", return_value=OPEN_OK) as run:
            result = brain.run_turn("open github in safari", self.ctx)
        self.assertEqual(run.call_args[0][0], ["open", "-a", "Safari", "https://github.com"])
        self.assertEqual(result.text, "GitHub is open in Safari.")

    def test_terminal_chat_protects_its_own_window(self):
        api = FakeAPI([reply([tool("toolu_t", "type_text", {"text": "hello"})], "tool_use"),
                       reply([text("Please click into the app first.")])])
        with mock.patch.object(mac, "frontmost_app", return_value=TERMINAL):
            memory = Memory(Path(self.tmp) / "m2.json")
            chat = cli.Chat(self.cfg, ui=self.ui, memory=memory,
                            brain=Brain(self.cfg, self.reg, memory, client=api.client()))
            self.assertEqual(chat.host_app["pid"], 111)
            with mock.patch.object(mac, "accessibility_trusted", return_value=True), \
                    mock.patch.object(mac, "paste_text") as paste:
                chat.ask("type hello")
        paste.assert_not_called()
        result = api.requests[1]["messages"][-1]["content"][0]
        self.assertTrue(result["is_error"])
        self.assertIn("terminal running Jarvis", result["content"])


if __name__ == "__main__":
    unittest.main()
