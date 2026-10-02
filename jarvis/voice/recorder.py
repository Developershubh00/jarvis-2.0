"""Microphone capture with simple voice-activity detection (stops when you stop talking)."""
from __future__ import annotations

import logging
import math
import queue
import threading
import time
from typing import Callable

import numpy as np

log = logging.getLogger(__name__)

SAMPLE_RATE = 16_000
BLOCK_SECONDS = 0.03
IGNORE_START_SECONDS = 0.25   # skip the start chime
MIN_VOICED_SECONDS = 0.25


class MicrophoneError(Exception):
    pass


MIC_PERMISSION_HINT = ("The microphone is giving pure silence. Allow Microphone access for your terminal app in "
                       "System Settings > Privacy & Security > Microphone, then quit and reopen the terminal.")


def level_from_rms(rms: float) -> float:
    """Map RMS amplitude to a 0..1 meter value (-60 dB .. -10 dB)."""
    db = 20.0 * math.log10(max(rms, 1e-9))
    return float(min(1.0, max(0.0, (db + 60.0) / 50.0)))


def resample(data: np.ndarray, rate: int) -> np.ndarray:
    """Linear resampling of one block to 16 kHz (plenty for speech recognition)."""
    if rate == SAMPLE_RATE or len(data) == 0:
        return data
    count = max(1, int(round(len(data) * SAMPLE_RATE / rate)))
    positions = np.linspace(0, len(data) - 1, count)
    return np.interp(positions, np.arange(len(data)), data).astype(np.float32)


class Recorder:
    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.device = cfg.voice.input_device
        self.block = int(SAMPLE_RATE * BLOCK_SECONDS)

    def warmup(self) -> None:
        """Open the mic briefly so macOS asks for permission now rather than mid-request."""
        import sounddevice as sd

        try:
            with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32", device=self.device):
                time.sleep(0.2)
        except Exception as e:
            log.warning("Microphone warm-up failed: %s", e)

    def _open_stream(self, sd, callback):
        """Open the mic at 16 kHz, or at its own rate (resampled to 16 kHz) if it refuses 16 kHz."""
        try:
            return sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32", blocksize=self.block,
                                  device=self.device, callback=callback), SAMPLE_RATE
        except Exception as first:
            try:
                rate = int(sd.query_devices(self.device, kind="input")["default_samplerate"])
            except Exception:
                rate = SAMPLE_RATE
            if rate == SAMPLE_RATE:
                raise MicrophoneError(f"Couldn't open the microphone: {first}") from first
            try:
                stream = sd.InputStream(samplerate=rate, channels=1, dtype="float32",
                                        blocksize=int(rate * BLOCK_SECONDS), device=self.device, callback=callback)
            except Exception as e:
                raise MicrophoneError(f"Couldn't open the microphone: {e}") from e
            log.info("Microphone refused 16 kHz; recording at %d Hz and resampling", rate)
            return stream, rate

    def record(self, stop_event: threading.Event, abort_event: threading.Event,
               on_level: Callable[[float], None] | None = None, silence_stop: bool = True,
               start_timeout: float | None = None, max_seconds: float | None = None) -> np.ndarray | None:
        """Record until silence after speech, stop_event, timeout or max length.

        Returns mono float32 audio at 16 kHz, or None if nothing was said (or abort_event fired).
        """
        import sounddevice as sd

        v = self.cfg.voice
        start_timeout = float(start_timeout if start_timeout is not None else v.start_timeout_seconds)
        max_seconds = float(max_seconds if max_seconds is not None else v.max_record_seconds)
        silence_seconds = float(v.silence_seconds)
        base_threshold = float(v.vad_threshold)

        blocks: queue.Queue = queue.Queue()
        stream_rate = {"hz": SAMPLE_RATE}

        def callback(indata, frames, time_info, status) -> None:  # runs on the audio thread
            data = indata[:, 0].copy()
            if stream_rate["hz"] != SAMPLE_RATE:
                data = resample(data, stream_rate["hz"])
            blocks.put(data)

        chunks: list[np.ndarray] = []
        noise: list[float] = []
        elapsed = 0.0
        voiced = 0.0
        last_voice = 0.0
        speech_started = False
        any_signal = False
        last_level = 0.0
        stream, stream_rate["hz"] = self._open_stream(sd, callback)
        with stream:
            waited = 0.0
            while True:
                if abort_event.is_set():
                    return None
                try:
                    data = blocks.get(timeout=0.25)
                except queue.Empty:
                    waited += 0.25
                    if waited > 3.0 and not chunks:
                        raise MicrophoneError("No audio is arriving from the microphone. " + MIC_PERMISSION_HINT)
                    if stop_event.is_set():
                        break
                    continue
                waited = 0.0
                chunks.append(data)
                duration = len(data) / SAMPLE_RATE
                elapsed += duration
                rms = float(np.sqrt(np.mean(np.square(data)))) if len(data) else 0.0
                if rms > 0.0:
                    any_signal = True
                if on_level is not None:
                    level = level_from_rms(rms) if elapsed > IGNORE_START_SECONDS else 0.0
                    if abs(level - last_level) > 0.02:
                        last_level = level
                        on_level(level)
                if stop_event.is_set():
                    break
                if elapsed < IGNORE_START_SECONDS:
                    noise.append(rms)
                    continue
                floor = float(np.percentile(noise, 20)) if noise else 0.0
                # Adapt to the room, but never so far that speech starting right at the chime (or the
                # chime itself) raises the bar above normal speaking volume.
                threshold = min(max(base_threshold, floor * 3.0), base_threshold * 4.0)
                if rms >= threshold:
                    if not speech_started:
                        speech_started = True
                    voiced += duration
                    last_voice = elapsed
                elif not speech_started:
                    noise.append(rms)
                    if len(noise) > 200:
                        noise = noise[-200:]
                if elapsed > 1.5 and not any_signal:
                    raise MicrophoneError(MIC_PERMISSION_HINT)
                if silence_stop:
                    if not speech_started and elapsed > start_timeout:
                        break
                    if speech_started and elapsed - last_voice > silence_seconds:
                        break
                if elapsed >= max_seconds:
                    break
        if on_level is not None:
            on_level(0.0)
        if abort_event.is_set() or voiced < MIN_VOICED_SECONDS or not chunks:
            return None
        return np.concatenate(chunks).astype(np.float32)
