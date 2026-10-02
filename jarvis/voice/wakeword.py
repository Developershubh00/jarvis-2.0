"""Optional "Hey Jarvis" wake word, detected on your Mac with openWakeWord (install: ./setup.sh --wakeword).

It listens in a background thread and steps aside whenever Jarvis itself uses the microphone, so it
never hears Jarvis talking. Audio never leaves your Mac.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Callable

import numpy as np

from .recorder import SAMPLE_RATE, Recorder, resample

log = logging.getLogger(__name__)

INSTALL_HINT = "The wake word needs the openwakeword package. Run ./setup.sh --wakeword, then restart Jarvis."
RETRY_SECONDS = 5.0


def to_int16(block: np.ndarray) -> np.ndarray:
    """openWakeWord expects 16-bit samples."""
    return (np.clip(block, -1.0, 1.0) * 32767.0).astype(np.int16)


class WakeWordListener:
    def __init__(self, cfg, on_wake: Callable[[], None], on_error: Callable[[str], None] | None = None) -> None:
        self.cfg = cfg
        self.on_wake = on_wake
        self.on_error = on_error
        self.error: str | None = None
        self._paused = threading.Event()
        self._closed = threading.Event()   # set whenever our microphone stream is closed
        self._closed.set()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="jarvis-wakeword", daemon=True)
        self._thread.start()

    def pause(self, wait: float = 1.0) -> None:
        """Stop listening and wait (briefly) until the microphone is free for Jarvis."""
        self._paused.set()
        self._closed.wait(wait)

    def resume(self) -> None:
        if not self._stop.is_set():
            self._paused.clear()

    def stop(self) -> None:
        self._stop.set()
        self._paused.set()

    def _report(self, message: str) -> None:
        if message == self.error:
            return
        self.error = message
        log.warning(message)
        if self.on_error is not None:
            try:
                self.on_error(message)
            except Exception:
                log.exception("on_error failed")

    def _load_model(self):
        try:
            from openwakeword.model import Model
            from openwakeword.utils import download_models
        except ImportError:
            self._report(INSTALL_HINT)
            return None
        name = str(self.cfg.wake_word.model or "hey_jarvis")
        try:
            download_models([name])  # once; later starts use the copy on disk
        except Exception as e:
            log.warning("Wake word model download failed (fine if it was downloaded before): %s", e)
        try:
            return Model(wakeword_models=[name], inference_framework="onnx")
        except Exception as e:
            self._report(f"Couldn't load the wake word model '{name}': {e}")
            return None

    def _run(self) -> None:
        model = self._load_model()
        if model is None:
            return
        import sounddevice as sd

        threshold = float(self.cfg.wake_word.threshold)
        recorder = Recorder(self.cfg)
        log.info("Wake word listener ready (threshold %.2f)", threshold)
        while not self._stop.is_set():
            if self._paused.is_set():
                time.sleep(0.05)
                continue
            frames: queue.Queue = queue.Queue(maxsize=64)
            rate = {"hz": SAMPLE_RATE}

            def callback(indata, n, time_info, status) -> None:
                data = indata[:, 0].copy()
                if rate["hz"] != SAMPLE_RATE:
                    data = resample(data, rate["hz"])
                try:
                    frames.put_nowait(to_int16(data))
                except queue.Full:
                    pass

            self._closed.clear()
            try:
                stream, rate["hz"] = recorder._open_stream(sd, callback)  # same mic handling as recording
                with stream:
                    model.reset()
                    while not self._paused.is_set() and not self._stop.is_set():
                        try:
                            block = frames.get(timeout=0.5)
                        except queue.Empty:
                            continue
                        scores = model.predict(block)
                        score = max(scores.values()) if scores else 0.0
                        if score >= threshold:
                            log.info("Wake word detected (%.2f)", score)
                            self._paused.set()  # Jarvis resumes us when it's done
                            model.reset()
                            try:
                                self.on_wake()
                            except Exception:
                                log.exception("on_wake failed")
                            break
            except Exception as e:
                self._report(f"The wake word can't use the microphone: {e}")
                if self._stop.wait(RETRY_SECONDS):
                    break
            finally:
                self._closed.set()
