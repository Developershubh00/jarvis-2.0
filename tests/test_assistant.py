"""Phase 6 tests: spoken replies and the assistant engine (quick phrases, follow-up questions,
stopping, Esc, and the background worker the menu-bar app will use).

Speech and the microphone are simulated: nothing is spoken or recorded during the tests.
Run:  python -m unittest discover -s tests -v
"""
from __future__ import annotations

import io
import logging
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_brain import NOWHERE, FakeAPI, api_error, reply, text, tool  # noqa: E402

from jarvis import cli  # noqa: E402
from jarvis import config as config_module  # noqa: E402
from jarvis.assistant import Assistant, Job  # noqa: E402
from jarvis.brain import Brain  # noqa: E402
from jarvis.config import load_config  # noqa: E402
from jarvis.memory import Memory  # noqa: E402
from jarvis.tools import Cancelled, build_registry  # noqa: E402
from jarvis.ui import ConsoleUI  # noqa: E402
from jarvis.voice import tts  # noqa: E402
from jarvis.voice.tts import Speaker, available_voices, clean_for_speech  # noqa: E402

AUDIO = np.full(16_000, 0.1, np.float32)
TERMINAL = {"name": "Terminal", "pid": 111}


class FakeSpeaker:
    """Records what would have been said."""

    def __init__(self, enabled=True):
        self.enabled, self.said, self.stops = enabled, [], 0

    def speak(self, words, cancel_event=None, clean=True):
        self.said.append(clean_for_speech(words) if clean else words)
        return not (cancel_event is not None and cancel_event.is_set())

    def stop(self):
        self.stops += 1


class ScriptedRecorder:
    def __init__(self):
        self.calls = []

    def record(self, stop_event=None, abort_event=None, on_level=None, **kwargs):
        self.calls.append(kwargs)
        return AUDIO


class ScriptedSTT:
    loaded, model_name, download_size = True, "small.en", "about 480 MB"

    def __init__(self, *heard):
        self.heard = list(heard)

    def is_cached(self):
        return True

    def transcribe(self, audio):
        return self.heard.pop(0)


class Base(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for patcher in (mock.patch.dict(os.environ), mock.patch.object(config_module, "ENV_PATH", NOWHERE)):
            patcher.start()
            self.addCleanup(patcher.stop)
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-key-for-unit-tests"
        self.cfg = self.make_cfg()

    def make_cfg(self, **extra):
        overrides = {"paths": {"workspace": str(Path(self.tmp) / "ws")}, "voice": {"sounds": False}}
        for key, value in extra.items():
            overrides.setdefault(key, {}).update(value)
        return load_config(NOWHERE, overrides=overrides, data_dir=Path(self.tmp) / "data", local_path=NOWHERE)

    def make(self, responses, speaker=None, recorder=None, stt=None, registry=None, cfg=None):
        cfg = cfg or self.cfg
        self.api = FakeAPI(responses)
        self.out = io.StringIO()
        self.ui = ConsoleUI(stream=self.out, interactive=False, color=False)
        memory = Memory(Path(self.tmp) / "memory.json")
        registry = registry or build_registry(cfg)
        brain = Brain(cfg, registry, memory, client=self.api.client())
        self.speaker = speaker if speaker is not None else FakeSpeaker()
        return Assistant(cfg, self.ui, brain=brain, memory=memory, registry=registry, speaker=self.speaker,
                         recorder=recorder, transcriber=stt)

    def sent_text(self, n=0):
        return self.api.requests[n]["messages"][-1]["content"][-1]["text"]


class SpeechOutputTests(Base):
    def test_replies_are_cleaned_up_for_speaking(self):
        out = clean_for_speech("## Done\n- **Created** `app.py`\n- See https://example.com\n```python\nprint(1)\n```")
        for leftover in ("**", "`", "http", "#"):
            self.assertNotIn(leftover, out)
        self.assertIn("Created app.py.", out)
        self.assertIn("the link", out)
        self.assertTrue(out.endswith("I've put the code on screen."))
        long = clean_for_speech("This is a sentence. " * 80)
        self.assertLessEqual(len(long), 650)
        self.assertTrue(long.endswith("The rest is on screen."))
        self.assertEqual(clean_for_speech("All set 🎉"), "All set.")

    def fake_say(self, polls=2):
        class Pipe(io.BytesIO):
            def close(self):
                self.data = self.getvalue()
                super().close()

        class Proc:
            def __init__(self):
                self.stdin, self.returncode, self.polls, self.terminated = Pipe(), None, polls, False

            def poll(self):
                if self.terminated:
                    self.returncode = -15
                elif self.polls <= 0:
                    self.returncode = 0
                else:
                    self.polls -= 1
                return self.returncode

            def terminate(self):
                self.terminated = True

        return Proc()

    def test_speaks_with_the_macos_say_command(self):
        proc = self.fake_say()
        with mock.patch.object(tts.shutil, "which", return_value="/usr/bin/say"), \
                mock.patch.object(tts, "available_voices", return_value=["Daniel", "Samantha"]), \
                mock.patch.object(tts.subprocess, "Popen", return_value=proc) as popen:
            self.assertTrue(Speaker(self.cfg, enabled=True).speak("**Done.** See https://example.com"))
        self.assertEqual(popen.call_args[0][0], ["say", "-r", "190", "-v", "Daniel"])
        self.assertEqual(proc.stdin.data, b"Done. See the link.")

    def test_missing_voice_falls_back_to_the_system_voice(self):
        with mock.patch.object(tts.shutil, "which", return_value="/usr/bin/say"), \
                mock.patch.object(tts, "available_voices", return_value=["Samantha"]), \
                mock.patch.object(tts.subprocess, "Popen", return_value=self.fake_say()) as popen:
            Speaker(self.cfg, enabled=True).speak("Hello.")
        self.assertEqual(popen.call_args[0][0], ["say", "-r", "190"])

    def test_speech_can_be_interrupted(self):
        proc = self.fake_say(polls=10_000)
        cancel = threading.Event()
        threading.Timer(0.2, cancel.set).start()
        with mock.patch.object(tts.shutil, "which", return_value="/usr/bin/say"), \
                mock.patch.object(tts, "available_voices", return_value=[]), \
                mock.patch.object(tts.subprocess, "Popen", return_value=proc):
            self.assertFalse(Speaker(self.cfg, enabled=True).speak("A long answer.", cancel_event=cancel))
        self.assertTrue(proc.terminated)

    def test_quiet_when_disabled_or_unavailable(self):
        with mock.patch.object(tts.shutil, "which", return_value=None), \
                mock.patch.object(tts.subprocess, "Popen") as popen:
            speaker = Speaker(self.cfg, enabled=True)
            self.assertFalse(speaker.enabled)
            self.assertTrue(speaker.speak("Hello."))
        popen.assert_not_called()

    def test_voice_list(self):
        listing = ("Daniel              en_GB    # Hello! My name is Daniel.\n"
                   "Eddy (English (UK)) en_GB    # Hello! My name is Eddy.\n"
                   "Rishi               en_IN    # Hello! My name is Rishi.\n")
        with mock.patch.object(tts.shutil, "which", return_value="/usr/bin/say"), \
                mock.patch.object(tts.subprocess, "run", return_value=mock.Mock(stdout=listing)):
            self.assertEqual(available_voices(), ["Daniel", "Eddy (English (UK))", "Rishi"])


class AssistantTests(Base):
    def test_typed_request_is_answered_and_spoken(self):
        a = self.make([reply([text("All done, sir.")])])
        self.assertEqual(a.process_text("tidy my desktop"), "All done, sir.")
        self.assertEqual(self.speaker.said, ["All done, sir."])
        self.assertIn("Reply: spoken aloud", self.sent_text())
        self.assertIn("Input: typed in the terminal", self.sent_text())
        self.assertNotIn("You said", self.out.getvalue())  # typed text isn't echoed back

    def test_quick_phrases_skip_the_api(self):
        a = self.make([])
        self.assertEqual(a.process_text("never mind"), "")
        self.assertEqual(a.process_text("Jarvis"), "Yes, sir?")
        self.assertEqual(a.process_text("repeat that"), "I haven't said anything yet.")
        self.assertEqual(a.process_text("Thanks!"), "Any time.")
        a.brain.history = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": [text("Hello")]}]
        self.assertEqual(a.process_text("Jarvis, start over please"), "Fresh start.")
        self.assertEqual(a.brain.history, [])
        self.assertEqual(self.api.requests, [])
        self.assertEqual(self.speaker.said, ["Yes, sir?", "I haven't said anything yet.", "Any time.", "Fresh start."])

    def test_errors_are_shown_and_their_first_sentence_spoken(self):
        a = self.make([api_error(401, "authentication_error", "invalid x-api-key")])
        self.assertEqual(a.process_text("hello"), "")
        self.assertIn("Error: My API key was rejected", self.out.getvalue())
        self.assertEqual(self.speaker.said[-1], "My API key was rejected.")
        self.assertEqual(a.state, "error")

    def test_follow_up_questions_get_an_answer(self):
        recorder = ScriptedRecorder()
        a = self.make([reply([text("Which folder should I use?")]), reply([text("Done, it's on your Desktop.")])],
                      recorder=recorder, stt=ScriptedSTT("make a to-do app", "the desktop"))
        self.assertEqual(a.listen_once(), "Which folder should I use?")
        self.assertTrue(a.follow_up_pending)
        self.assertEqual(a.listen_once(follow_up=True), "Done, it's on your Desktop.")
        self.assertFalse(a.follow_up_pending)
        self.assertIsNone(recorder.calls[0]["start_timeout"])
        self.assertEqual(recorder.calls[1]["start_timeout"], 6.0)  # a shorter wait for the answer
        self.assertIn("Input: spoken", self.sent_text(1))
        self.assertIn('You said: "the desktop"', self.out.getvalue())

    def test_terminal_voice_session_with_a_follow_up(self):
        recorder = ScriptedRecorder()
        speaker = FakeSpeaker()
        api = FakeAPI([reply([text("Which folder should I use?")]), reply([text("Done.")])])
        out = io.StringIO()
        ui = ConsoleUI(stream=out, interactive=False, color=False)
        memory = Memory(Path(self.tmp) / "m.json")
        chat = cli.Chat(self.cfg, ui=ui, memory=memory, speaker=speaker, recorder=recorder,
                        stt=ScriptedSTT("make a to-do app", "the desktop"),
                        brain=Brain(self.cfg, build_registry(self.cfg), memory, client=api.client()))
        with mock.patch("sys.stdout", io.StringIO()), mock.patch("builtins.input", side_effect=["", "/quit"]):
            self.assertEqual(cli.run_cli(self.cfg, chat=chat, voice=True), 0)
        self.assertEqual(speaker.said, ["Which folder should I use?", "Done."])
        self.assertEqual(len(recorder.calls), 2)  # it listened for the answer without another Enter

    def test_speak_tool_talks_during_a_task(self):
        script = [reply([tool("toolu_s", "speak", {"text": "Working on it."})], "tool_use"), reply([text("Finished.")])]
        a = self.make(list(script))
        a.process_text("build it")
        self.assertEqual(self.speaker.said, ["Working on it.", "Finished."])
        a = self.make(list(script), speaker=FakeSpeaker(enabled=False))
        a.process_text("build it")
        self.assertIn("Speech is off", self.api.requests[1]["messages"][-1]["content"][0]["content"])

    def test_ctrl_c_stops_a_long_task(self):
        registry = build_registry(self.cfg)

        def slow(ctx, args):
            threading.Timer(0.2, assistant.stop_now).start()
            ctx.cancel.wait(5)
            raise Cancelled()

        registry.add("slow", "takes a while", {}, func=slow)
        assistant = self.make([reply([tool("toolu_x", "slow", {})], "tool_use")], registry=registry)
        started = time.monotonic()
        self.assertEqual(assistant.process_text("do the slow thing"), "")
        self.assertLess(time.monotonic() - started, 3)
        self.assertIn("Stopped.", self.out.getvalue())
        self.assertEqual(assistant.state, "idle")

    def test_escape_needs_a_second_press_while_working(self):
        a = self.make([])
        a.state = "thinking"
        a.on_cancel()
        self.assertFalse(a.cancel.is_set())
        self.assertIn("Press Esc again to stop.", self.out.getvalue())
        a.on_cancel()
        self.assertTrue(a.cancel.is_set())
        a.cancel.clear()
        a.state = "listening"
        a.current_listen = Job("listen")
        a.on_cancel()  # while listening or speaking, one press is enough
        self.assertTrue(a.cancel.is_set())

    def test_pressing_the_hotkey_again_finishes_the_recording(self):
        a = self.make([])
        a.state = "listening"
        job = a.current_listen = Job("listen")
        a.on_talk()
        self.assertTrue(job.stop.is_set())

    def test_background_worker_runs_typed_requests(self):
        a = self.make([reply([text("Four.")])])
        self.ui.ask_text = lambda prompt: "what's two plus two"
        a.start(preload=False)
        self.addCleanup(a.shutdown)
        a.on_type()
        deadline = time.monotonic() + 5
        while (a.state != "idle" or not self.speaker.said) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(self.speaker.said, ["Four."])
        self.assertIn("Input: typed in the Jarvis prompt", self.sent_text())

    def test_terminal_is_never_the_target_app(self):
        a = self.make([])
        a.host_app = TERMINAL
        self.assertIsNone(a._frontmost())

    def test_wake_word_setting_before_it_exists(self):
        a = self.make([], cfg=self.make_cfg(wake_word={"enabled": True}))
        with mock.patch.dict(sys.modules, {"jarvis.voice.wakeword": None}):  # the module arrives in phase 10
            a.start(preload=False)
        self.addCleanup(a.shutdown)
        self.assertIn("wake word files are missing", self.out.getvalue())


if __name__ == "__main__":
    unittest.main()
