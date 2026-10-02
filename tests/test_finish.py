"""Phase 10 tests: the "Hey Jarvis" wake word, starting at login, and the final checks.

The wake word model, the microphone and macOS's Login Items are simulated: nothing listens for real
and nothing is added to your login items.  Run:  python -m unittest discover -s tests -v
"""
from __future__ import annotations

import io
import logging
import os
import shlex
import shutil
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_assistant import FakeSpeaker  # noqa: E402
from test_brain import NOWHERE, FakeAPI  # noqa: E402
from test_voice import SPEECH, FakeMicrophone, FakeRecorder, FakeSTT  # noqa: E402

from jarvis import config as config_module  # noqa: E402
from jarvis import login, mac  # noqa: E402
from jarvis.__main__ import main  # noqa: E402
from jarvis.assistant import Assistant  # noqa: E402
from jarvis.brain import Brain  # noqa: E402
from jarvis.config import load_config  # noqa: E402
from jarvis.doctor import FAIL, INFO, Report, check_extras  # noqa: E402
from jarvis.memory import Memory  # noqa: E402
from jarvis.tools import build_registry  # noqa: E402
from jarvis.ui import ConsoleUI  # noqa: E402
from jarvis.voice.wakeword import INSTALL_HINT, WakeWordListener, to_int16  # noqa: E402


class FakeWakeModel:
    """Stands in for openWakeWord: 'hears' the wake word on the fifth block of audio."""

    def __init__(self, fire_on=5, score=0.9):
        self.fire_on, self.score = fire_on, score
        self.calls = self.resets = 0
        self.dtypes, self.lengths = set(), set()

    def predict(self, block):
        self.calls += 1
        self.dtypes.add(block.dtype)
        self.lengths.add(len(block))
        return {"hey_jarvis_v0.1": self.score if self.calls == self.fire_on else 0.01}

    def reset(self):
        self.resets += 1


def fake_openwakeword(model):
    calls = []
    package = types.ModuleType("openwakeword")
    model_module, utils = types.ModuleType("openwakeword.model"), types.ModuleType("openwakeword.utils")

    def make_model(wakeword_models, inference_framework):
        calls.append(("model", wakeword_models, inference_framework))
        return model

    model_module.Model = make_model
    utils.download_models = lambda names: calls.append(("download", names))
    package.model, package.utils = model_module, utils
    return {"openwakeword": package, "openwakeword.model": model_module, "openwakeword.utils": utils}, calls


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
        for section, values in extra.items():
            overrides.setdefault(section, {}).update(values)
        cfg = load_config(NOWHERE, overrides=overrides, data_dir=Path(self.tmp) / "data", local_path=NOWHERE)
        cfg.paths.project_root = Path(self.tmp)
        return cfg


class WakeWordTests(Base):
    def listen(self, model, mic, on_error=None, cfg=None):
        modules, self.calls = fake_openwakeword(model)
        modules["sounddevice"] = mic.module()
        patcher = mock.patch.dict(sys.modules, modules)
        patcher.start()
        self.addCleanup(patcher.stop)
        heard = threading.Event()
        listener = WakeWordListener(cfg or self.cfg, on_wake=heard.set, on_error=on_error)
        listener.start()
        self.addCleanup(listener.stop)
        return listener, heard

    def test_hey_jarvis_wakes_jarvis_then_steps_aside(self):
        model = FakeWakeModel()
        listener, heard = self.listen(model, FakeMicrophone([], tail=SPEECH))
        self.assertTrue(heard.wait(3))
        self.assertIn(("download", ["hey_jarvis"]), self.calls)
        self.assertIn(("model", ["hey_jarvis"], "onnx"), self.calls)
        self.assertEqual(model.dtypes, {np.dtype(np.int16)})
        listener.pause()  # Jarvis takes the microphone...
        self.assertTrue(listener._closed.is_set())
        before = model.calls
        time.sleep(0.2)
        self.assertEqual(model.calls, before)  # ...and the wake word stays out of the way
        heard.clear()
        model.calls = 0
        listener.resume()
        self.assertTrue(heard.wait(3))  # listening again once Jarvis is done

    def test_quiet_scores_do_not_wake_it(self):
        listener, heard = self.listen(FakeWakeModel(score=0.3), FakeMicrophone([], tail=SPEECH))
        self.assertFalse(heard.wait(0.5))  # the default threshold is 0.5

    def test_microphone_that_refuses_16_khz(self):
        model = FakeWakeModel()
        listener, heard = self.listen(model, FakeMicrophone([], tail=SPEECH, refuse_16k=True, native_rate=48_000))
        self.assertTrue(heard.wait(3))
        self.assertEqual(model.lengths, {480})  # 30 ms blocks, resampled to 16 kHz

    def test_missing_package_is_explained(self):
        errors = []
        patcher = mock.patch.dict(sys.modules, {"openwakeword": None, "openwakeword.model": None,
                                                "openwakeword.utils": None})
        patcher.start()
        self.addCleanup(patcher.stop)
        listener = WakeWordListener(self.cfg, on_wake=lambda: None, on_error=errors.append)
        listener.start()
        listener._thread.join(2)
        self.assertEqual(errors, [INSTALL_HINT])

    def test_samples_are_converted_safely(self):
        self.assertEqual(to_int16(np.array([0.0, 1.5, -2.0], np.float32)).tolist(), [0, 32767, -32767])


class FakeListener:
    instances: list = []

    def __init__(self, cfg, on_wake, on_error=None):
        self.on_wake, self.on_error, self.events = on_wake, on_error, []
        FakeListener.instances.append(self)

    def start(self):
        self.events.append("start")

    def pause(self, wait=1.0):
        self.events.append("pause")

    def resume(self):
        self.events.append("resume")

    def stop(self):
        self.events.append("stop")


class AssistantWakeWordTests(Base):
    def test_wake_word_starts_listening_and_pauses_while_jarvis_works(self):
        module = types.ModuleType("jarvis.voice.wakeword")
        module.WakeWordListener = FakeListener
        cfg = self.make_cfg(wake_word={"enabled": True})
        out = io.StringIO()
        memory = Memory(Path(self.tmp) / "m.json")
        brain = Brain(cfg, build_registry(cfg), memory, client=FakeAPI([]).client())
        assistant = Assistant(cfg, ConsoleUI(stream=out, interactive=False, color=False), brain=brain,
                              memory=memory, speaker=FakeSpeaker(), recorder=FakeRecorder(None), transcriber=FakeSTT("x"))
        with mock.patch.dict(sys.modules, {"jarvis.voice.wakeword": module}):
            assistant.start(preload=False)
        self.addCleanup(assistant.shutdown)
        listener = FakeListener.instances[-1]
        self.assertEqual(listener.events, ["start"])
        listener.on_wake()  # "Hey Jarvis"
        deadline = time.monotonic() + 3
        while "resume" not in listener.events and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(listener.events, ["start", "pause", "resume"])
        listener.on_error("The wake word can't use the microphone: busy")
        self.assertIn("can't use the microphone", out.getvalue())
        assistant.state = "thinking"
        listener.on_wake()  # busy: ignored
        self.assertTrue(assistant.jobs.empty())


class LoginTests(Base):
    def test_command_file_runs_jarvis_from_its_folder(self):
        path = login.write_command(self.cfg)
        self.assertEqual(path.name, "Jarvis.command")
        self.assertTrue(path.read_text().startswith("#!/bin/bash"))
        self.assertIn(f"cd {shlex.quote(self.tmp)} && exec ./run.sh", path.read_text())
        self.assertTrue(os.access(path, os.X_OK))

    def test_turning_it_on_and_off(self):
        item = str(Path(self.tmp) / "Jarvis.command")
        with mock.patch.object(login.sys, "platform", "darwin"), \
                mock.patch.object(mac, "osascript", return_value="") as osascript:
            worked, message = login.set_login(self.cfg, True)
            self.assertTrue(worked)
            self.assertIn("start when you log in", message)
            script = osascript.call_args[0][0]
            self.assertIn("make login item", script)
            self.assertIn(item, script)
            self.assertTrue(login.set_login(self.cfg, False)[0])
            self.assertIn("delete (every login item whose path is", osascript.call_args[0][0])
            osascript.return_value = f"/Applications/Other.app, {item}"
            self.assertTrue(login.login_status(self.cfg))

    def test_without_permission_it_explains_the_manual_way(self):
        denied = mac.AppleScriptError("Not authorized to send Apple events to System Events. (-1743)")
        with mock.patch.object(login.sys, "platform", "darwin"), mock.patch.object(mac, "osascript", side_effect=denied):
            worked, message = login.set_login(self.cfg, True)
            self.assertIsNone(login.login_status(self.cfg))
        self.assertFalse(worked)
        self.assertIn("Automation", message)
        self.assertIn("Login Items", message)

    def test_command_line(self):
        printed = io.StringIO()
        with mock.patch("sys.stdout", printed), mock.patch.object(config_module, "ensure_dirs"), \
                mock.patch.object(config_module, "setup_logging"), \
                mock.patch.object(login, "set_login", return_value=(True, "Jarvis will start when you log in.")) as turn:
            self.assertEqual(main(["--login", "on"]), 0)
        self.assertTrue(turn.call_args[0][1])
        self.assertIn("start when you log in", printed.getvalue())


class FinalDoctorTests(Base):
    def test_extras(self):
        out = io.StringIO()
        r = Report(out, color=False)
        check_extras(r, self.cfg)
        self.assertEqual([(s, label) for s, label, _ in r.items],
                         [(INFO, "Wake word: off"), (INFO, "Start at login: off")])
        login.write_command(self.cfg)
        r = Report(io.StringIO(), color=False)
        with mock.patch.dict(sys.modules, {"openwakeword": None}):
            check_extras(r, self.make_cfg(wake_word={"enabled": True}))
        self.assertEqual([s for s, _, _ in r.items], [FAIL, INFO])
        self.assertIn("set up", r.items[1][1])


if __name__ == "__main__":
    unittest.main()
