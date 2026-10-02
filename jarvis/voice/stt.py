"""Speech-to-text with faster-whisper, running locally on the CPU."""
from __future__ import annotations

import logging
import os
import re
import threading
from pathlib import Path

log = logging.getLogger(__name__)

# Whisper sometimes "hears" these in noise or silence.
HALLUCINATIONS = {
    "you", "thanks for watching", "thank you for watching", "please subscribe", "subtitles by the amara org community",
    "music", "applause", "silence", "bye", "uh", "um", "hmm", "so", "oh", "ah",
}


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9' ]", " ", text.lower())).strip()


class Transcriber:
    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.model_name = str(cfg.voice.stt_model or "small.en")
        self.models_dir = Path(cfg.paths.models)
        self._model = None
        self._lock = threading.Lock()

    @property
    def loaded(self) -> bool:
        return self._model is not None

    @property
    def download_size(self) -> str:
        name = self.model_name.lower()
        for key, size in (("tiny", "75 MB"), ("base", "145 MB"), ("small", "480 MB"), ("medium", "1.5 GB"),
                          ("turbo", "1.6 GB"), ("large", "3 GB")):
            if key in name:
                return f"about {size}"
        return "a few hundred MB"

    def is_cached(self) -> bool:
        name = self.model_name.split("/")[-1]
        return any(self.models_dir.glob(f"models--*{name}*")) or Path(self.model_name).is_dir()

    def load(self):
        with self._lock:
            if self._model is None:
                from faster_whisper import WhisperModel

                threads = max(2, min(8, (os.cpu_count() or 4) // 2))
                log.info("Loading speech model %s (%d threads)", self.model_name, threads)
                self._model = WhisperModel(self.model_name, device="cpu", compute_type="int8",
                                           cpu_threads=threads, download_root=str(self.models_dir))
            return self._model

    def transcribe(self, audio) -> str:
        model = self.load()
        v = self.cfg.voice
        language = v.language or None
        if language and self.model_name.endswith(".en") and language != "en":
            language = "en"
        segments, _info = model.transcribe(
            audio,
            language=language,
            beam_size=int(v.beam_size),
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
            initial_prompt=(v.vocabulary or None),
            condition_on_previous_text=False,
            without_timestamps=True,
        )
        text = " ".join(seg.text.strip() for seg in segments).strip()
        heard = normalize(text)
        if not heard or heard in HALLUCINATIONS:
            log.info("Ignoring empty or noise transcript: %r", text)
            return ""
        if len(heard.split()) >= 3 and heard in normalize(v.vocabulary or ""):
            log.info("Ignoring a transcript that just repeats the vocabulary hint: %r", text)
            return ""
        log.info("Heard: %s", text)
        return text
