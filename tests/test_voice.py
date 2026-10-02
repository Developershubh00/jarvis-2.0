"""Phase 5 tests: the microphone recorder, speech recognition and talking in the terminal.

The microphone and the Whisper model are simulated, so these run anywhere (GitHub CI included),
never record real audio and never download anything.  Run:  python -m unittest discover -s tests -v
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
import types
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_brain import NOWHERE, FakeAPI, reply, text  # noqa: E402

from jarvis import cli  # noqa: E402
from jarvis import config as config_module  # noqa: E402
from jarvis.brain import Brain  # noqa: E402
from jarvis.config import load_config  # noqa: E402
from jarvis.doctor import FAIL, OK, Report, check_voice  # noqa: E402
from jarvis.memory import Memory  # noqa: E402
from jarvis.tools import build_registry  # noqa: E402
from jarvis.ui import ConsoleUI  # noqa: E402
from jarvis.voice.recorder import MicrophoneError, Recorder, level_from_rms, resample  # noqa: E402
from jarvis.voice.stt import HALLUCINATIONS, Transcriber, normalize  # noqa: E402

QUIET, SPEECH = 0.002, 0.1  # loudness (RMS) of a quiet room and of someone talking


class FakeMicrophone:
    """Stands in for the sounddevice module: plays a script of (seconds, loudness), then keeps
    going at the tail loudness like a real mic, about 100x faster than real time."""

    class PortAudioError(Exception):
        pass

    def __init__(self, script, tail=QUIET, refuse_16k=False, native_rate=48_000, stop=None, stop_after=None):
        self.script, self.tail = list(script), tail
        self.refuse_16k, self.native_rate = refuse_16k, native_rate
        self.stop, self.stop_after = stop, stop_after
        self.opened_rates: list[int] = []

    def module(self):
        mod = types.ModuleType("sounddevice")
        mod.PortAudioError = self.PortAudioError
        mod.InputStream = self.input_stream
        mod.query_devices = lambda device=None, kind=None: {"name": "Test Mic", "default_samplerate": float(self.native_rate)}
        return mod

    def input_stream(self, samplerate, channels, dtype, blocksize=None, device=None, callback=None):
        if self.refuse_16k and samplerate == 16_000:
            raise self.PortAudioError("Invalid sample rate")
        self.opened_rates.append(int(samplerate))
        return _FakeStream(self, int(samplerate), int(blocksize or samplerate * 0.03), callback)

    def blocks(self, rate, size):
        rng = np.random.default_rng(0)
        for seconds, level in self.script + [(None, self.tail)]:
            count = None if seconds is None else int(seconds * rate / size)
            n = 0
            while count is None or n < count:
                yield (rng.standard_normal((size, 1)) * level).astype(np.float32)
                n += 1


class _FakeStream:
    def __init__(self, mic, rate, size, callback):
        self.mic, self.rate, self.size, self.callback = mic, rate, size, callback
        self.running = False

    def __enter__(self):
        self.running = True
        self.thread = threading.Thread(target=self.feed, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.running = False
        self.thread.join(1)

    def feed(self):
        for n, block in enumerate(self.mic.blocks(self.rate, self.size), start=1):
            if not self.running:
                return
            self.callback(block, len(block), None, None)
            if self.mic.stop is not None and n == self.mic.stop_after:
                self.mic.stop.set()
            time.sleep(0.0003)


class FakeRecorder:
    def __init__(self, result):
        self.result = result

    def record(self, stop, abort, on_level=None, **kwargs):
        if isinstance(self.result, BaseException):
            raise self.result
        if on_level:
            on_level(0.7)
            on_level(0.0)
        return self.result


class FakeSTT:
    model_name, download_size = "small.en", "about 480 MB"

    def __init__(self, words, loaded=True, cached=True):
        self.words, self.loaded, self.cached = words, loaded, cached

    def is_cached(self):
        return self.cached

    def load(self):
        self.loaded = True

    def transcribe(self, audio):
        return self.words


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
        overrides = {"paths": {"workspace": str(Path(self.tmp) / "ws")},
                     "voice": {"sounds": False, "start_timeout_seconds": 2, "silence_seconds": 0.6}}
        self.cfg = load_config(NOWHERE, overrides=overrides, data_dir=Path(self.tmp) / "data", local_path=NOWHERE)

    def record(self, mic, stop=None, abort=None, **kwargs):
        with mock.patch.dict(sys.modules, {"sounddevice": mic.module()}):
            return Recorder(self.cfg).record(stop or threading.Event(), abort or threading.Event(), **kwargs)


class RecorderTests(Base):
    def test_records_until_you_stop_talking(self):
        audio = self.record(FakeMicrophone([(0.5, QUIET), (1.5, SPEECH), (3.0, QUIET)]))
        self.assertIsNotNone(audio)
        self.assertEqual(audio.dtype, np.float32)
        seconds = len(audio) / 16_000
        self.assertGreater(seconds, 2.0)  # 0.5 s lead-in + 1.5 s of speech
        self.assertLess(seconds, 3.2)     # + about 0.6 s of silence, not the whole 3 s

    def test_silence_gives_nothing(self):
        self.assertIsNone(self.record(FakeMicrophone([], tail=QUIET)))

    def test_denied_microphone_permission_is_explained(self):
        with self.assertRaises(MicrophoneError) as caught:
            self.record(FakeMicrophone([], tail=0.0))
        self.assertIn("Privacy & Security", str(caught.exception))

    def test_stop_abort_and_max_length(self):
        stop = threading.Event()
        audio = self.record(FakeMicrophone([], tail=SPEECH, stop=stop, stop_after=50), stop=stop)
        self.assertAlmostEqual(len(audio) / 16_000, 1.5, delta=0.5)  # 50 blocks of 30 ms
        aborted = threading.Event()
        aborted.set()
        self.assertIsNone(self.record(FakeMicrophone([], tail=SPEECH), abort=aborted))
        audio = self.record(FakeMicrophone([], tail=SPEECH), max_seconds=2.0)
        self.assertAlmostEqual(len(audio) / 16_000, 2.0, delta=0.1)

    def test_level_meter(self):
        levels: list[float] = []
        self.record(FakeMicrophone([(0.5, QUIET), (1.0, SPEECH), (2.0, QUIET)]), on_level=levels.append)
        self.assertGreater(max(levels), 0.5)
        self.assertEqual(levels[-1], 0.0)
        self.assertTrue(all(0.0 <= v <= 1.0 for v in levels))

    def test_microphone_that_refuses_16_khz(self):
        mic = FakeMicrophone([(0.5, QUIET), (1.0, SPEECH), (2.0, QUIET)], refuse_16k=True, native_rate=48_000)
        audio = self.record(mic)
        self.assertEqual(mic.opened_rates, [48_000])
        self.assertTrue(1.5 < len(audio) / 16_000 < 2.6)

    def test_resampling_and_meter_math(self):
        self.assertEqual(len(resample(np.zeros(1440, np.float32), 48_000)), 480)
        self.assertEqual(level_from_rms(0.0), 0.0)
        self.assertEqual(level_from_rms(1.0), 1.0)
        self.assertTrue(0.3 < level_from_rms(0.02) < 0.7)


class SpeechRecognitionTests(Base):
    def whisper(self, *pieces):
        created = []

        class Model:
            def __init__(self, name, **kwargs):
                self.name, self.kwargs, self.calls = name, kwargs, []
                created.append(self)

            def transcribe(self, audio, **kwargs):
                self.calls.append(kwargs)
                return iter([types.SimpleNamespace(text=p) for p in pieces]), types.SimpleNamespace(language="en")

        module = types.ModuleType("faster_whisper")
        module.WhisperModel = Model
        return module, created

    def test_transcribes_with_the_local_model(self):
        module, created = self.whisper(" Open Safari", " and GitHub. ")
        with mock.patch.dict(sys.modules, {"faster_whisper": module}):
            stt = Transcriber(self.cfg)
            self.assertFalse(stt.loaded)
            self.assertEqual(stt.transcribe(np.zeros(16_000, np.float32)), "Open Safari and GitHub.")
            stt.transcribe(np.zeros(16_000, np.float32))
        self.assertEqual(len(created), 1)  # loaded once, then reused
        model = created[0]
        self.assertEqual(model.name, "small.en")
        self.assertEqual(model.kwargs["compute_type"], "int8")
        self.assertEqual(model.kwargs["download_root"], str(self.cfg.paths.models))
        self.assertEqual(model.calls[0]["language"], "en")
        self.assertTrue(model.calls[0]["vad_filter"])

    def test_noise_and_phantom_text_are_dropped(self):
        for heard in (" Thanks for watching! ", "you", "", self.cfg.voice.vocabulary):
            module, _ = self.whisper(heard)
            with mock.patch.dict(sys.modules, {"faster_whisper": module}):
                self.assertEqual(Transcriber(self.cfg).transcribe(np.zeros(10, np.float32)), "", heard)
        self.assertIn(normalize(" Thanks for watching! "), HALLUCINATIONS)
        self.assertNotIn(normalize("Open Safari"), HALLUCINATIONS)

    def test_model_download_status(self):
        stt = Transcriber(self.cfg)
        self.assertFalse(stt.is_cached())
        self.assertEqual(stt.download_size, "about 480 MB")
        Path(self.cfg.paths.models, "models--Systran--faster-whisper-small.en").mkdir(parents=True)
        self.assertTrue(stt.is_cached())


class TalkingTests(Base):
    def chat(self, responses, recorder, stt):
        api = FakeAPI(responses)
        out = io.StringIO()
        ui = ConsoleUI(stream=out, interactive=False, color=False)
        memory = Memory(Path(self.tmp) / "memory.json")
        brain = Brain(self.cfg, build_registry(self.cfg), memory, client=api.client())
        return api, cli.Chat(self.cfg, ui=ui, brain=brain, memory=memory, recorder=recorder, stt=stt), out

    def test_spoken_request_reaches_jarvis(self):
        api, chat, out = self.chat([reply([text("Opening it now.")])],
                                   FakeRecorder(np.full(16_000, 0.1, np.float32)), FakeSTT("open github"))
        printed = io.StringIO()
        with mock.patch("sys.stdout", printed), mock.patch("builtins.input", side_effect=["", "/quit"]):
            self.assertEqual(cli.run_cli(self.cfg, chat=chat, voice=True), 0)
        shown = out.getvalue()
        self.assertIn('You said: "open github"', shown)
        self.assertIn("jarvis › Opening it now.", shown)
        self.assertIn("press Enter on an empty line", printed.getvalue())
        sent = api.requests[0]["messages"][0]["content"][0]["text"]
        self.assertIn("Input: spoken", sent)
        self.assertIn("Reply: shown as text", sent)
        self.assertTrue(sent.endswith("open github"))

    def test_problems_while_listening_never_reach_the_api(self):
        cases = ((None, "didn't hear anything"), (MicrophoneError("pure silence"), "Error: pure silence"),
                 (KeyboardInterrupt(), "stopped listening"))
        for result, expected in cases:
            api, chat, out = self.chat([], FakeRecorder(result), FakeSTT("x"))
            self.assertIsNone(chat.listen())
            self.assertIn(expected, out.getvalue())
            self.assertEqual(api.requests, [])
        api, chat, out = self.chat([], FakeRecorder(np.ones(16_000, np.float32)), FakeSTT(""))
        self.assertIsNone(chat.listen())
        self.assertIn("couldn't make out any words", out.getvalue())

    def test_microphone_test_needs_no_api_key(self):
        os.environ["ANTHROPIC_API_KEY"] = ""
        out, printed = io.StringIO(), io.StringIO()
        ui = ConsoleUI(stream=out, interactive=False, color=False)
        with mock.patch("sys.stdout", printed), mock.patch("builtins.input", side_effect=["", EOFError()]):
            code = cli.run_listen(self.cfg, recorder=FakeRecorder(np.ones(16_000, np.float32)),
                                  stt=FakeSTT("testing one two three"), ui=ui)
        self.assertEqual(code, 0)
        self.assertIn('You said: "testing one two three"', out.getvalue())
        self.assertIn("no API key needed", printed.getvalue())

    def test_first_use_downloads_the_model(self):
        printed = io.StringIO()
        with mock.patch("sys.stdout", printed):
            self.assertTrue(cli.load_speech(FakeSTT("x", loaded=False, cached=False)))
            broken = FakeSTT("x", loaded=False, cached=False)
            broken.load = mock.Mock(side_effect=OSError("offline"))
            self.assertFalse(cli.load_speech(broken))
        self.assertIn("Downloading the speech model small.en (about 480 MB)", printed.getvalue())
        self.assertIn("internet connection", printed.getvalue())

    def test_voice_mode_without_a_key_suggests_the_mic_test(self):
        os.environ["ANTHROPIC_API_KEY"] = ""
        printed = io.StringIO()
        with mock.patch("sys.stdout", printed):
            self.assertEqual(cli.run_cli(self.cfg, voice=True), 1)
        self.assertIn("./run.sh --listen", printed.getvalue())


class DoctorMicrophoneTests(Base):
    def test_microphone_check(self):
        sd = types.ModuleType("sounddevice")
        sd.query_devices = lambda device=None, kind=None: {"name": "MacBook Pro Microphone"}
        sd.wait = lambda: None
        for level, expected in ((0.05, OK), (0.0, FAIL)):
            sd.rec = lambda frames, level=level, **kw: np.full((frames, 1), level, np.float32)
            out = io.StringIO()
            r = Report(out, color=False)
            with mock.patch.dict(sys.modules, {"sounddevice": sd}):
                check_voice(r, self.cfg)
            self.assertEqual(r.items[-1][0], expected)
            self.assertIn("MacBook Pro Microphone", out.getvalue())
        self.assertIn("Privacy & Security", out.getvalue())

    def test_hardware_check_can_be_skipped(self):
        r = Report(io.StringIO(), color=False)
        with mock.patch.dict(sys.modules, {"sounddevice": None}):
            check_voice(r, self.cfg, hardware=False)
        self.assertEqual([status for status, _, _ in r.items], ["info"])


if __name__ == "__main__":
    unittest.main()
