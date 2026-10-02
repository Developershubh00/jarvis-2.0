"""Text-to-speech with the built-in macOS `say` command (interruptible)."""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
import threading
import time

log = logging.getLogger(__name__)

EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U0001F000-\U0001F2FF\U00002600-\U000027BF\U0001F900-\U0001F9FF\uFE0F\u200D]")
SPEECH_LIMIT = 600


def clean_for_speech(text: str, limit: int = SPEECH_LIMIT) -> str:
    """Turn a chat-style reply into something pleasant to hear."""
    if not text:
        return ""
    had_code = False

    def drop_code(_m: re.Match) -> str:
        nonlocal had_code
        had_code = True
        return "\n"

    t = re.sub(r"```.*?(?:```|$)", drop_code, text, flags=re.S)
    t = re.sub(r"`([^`\n]*)`", r"\1", t)
    t = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"\[([^\]]+)\]\((?:https?|mailto):[^)]*\)", r"\1", t)
    t = re.sub(r"https?://\S+", "the link", t)
    t = re.sub(r"^\s{0,3}#{1,6}\s*", "", t, flags=re.M)
    t = re.sub(r"^\s*\|.*\|\s*$", "", t, flags=re.M)
    t = re.sub(r"^\s*[-*+•]\s+", "", t, flags=re.M)
    t = re.sub(r"^\s*\d+[.)]\s+", "", t, flags=re.M)
    t = re.sub(r"^\s*>\s?", "", t, flags=re.M)
    t = re.sub(r"(\*\*|__)(.+?)\1", r"\2", t)
    t = re.sub(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])", r"\1", t)
    t = EMOJI_RE.sub("", t)
    sentences = []
    for line in t.splitlines():
        line = line.strip()
        if not line:
            continue
        if line[-1] not in ".!?:;,":
            line += "."
        sentences.append(line)
    t = re.sub(r"\s+", " ", " ".join(sentences)).strip()
    if had_code:
        t = (t + " I've put the code on screen.").strip()
    if len(t) > limit:
        cut = max(t.rfind(". ", 0, limit), t.rfind("? ", 0, limit), t.rfind("! ", 0, limit))
        t = (t[: cut + 1] if cut > limit // 3 else t[:limit].rsplit(" ", 1)[0] + ".") + " The rest is on screen."
    return t


def available_voices() -> list[str]:
    if not shutil.which("say"):
        return []
    try:
        out = subprocess.run(["say", "-v", "?"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    names = []
    for line in out.splitlines():
        m = re.match(r"^(.+?)\s+([a-z]{2,3}[_-][A-Za-z0-9]+)\s+#", line)
        if m:
            names.append(m.group(1).strip())
    return names


class Speaker:
    def __init__(self, cfg, enabled: bool | None = None) -> None:
        self.cfg = cfg
        self.available = shutil.which("say") is not None
        wanted = bool(cfg.voice.tts) if enabled is None else bool(enabled)
        self.enabled = wanted and self.available
        self.rate = int(cfg.voice.tts_rate)
        self.voice = str(cfg.voice.tts_voice or "") or None
        self._voice_checked = False
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._stopped = False

    def _check_voice(self) -> None:
        if self._voice_checked:
            return
        self._voice_checked = True
        if self.voice:
            voices = available_voices()
            if voices and self.voice not in voices:
                log.warning("Voice %r isn't installed; using the system voice", self.voice)
                self.voice = None

    def speak(self, text: str, cancel_event: threading.Event | None = None, clean: bool = True) -> bool:
        """Speak and block until done. Returns False if interrupted."""
        if not self.enabled:
            return True
        text = clean_for_speech(text) if clean else text
        if not text:
            return True
        self._check_voice()
        args = ["say", "-r", str(self.rate)]
        if self.voice:
            args += ["-v", self.voice]
        self.stop()
        try:
            proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as e:
            log.warning("say failed: %s", e)
            return True
        with self._lock:
            self._proc = proc
            self._stopped = False
        try:
            proc.stdin.write(text.encode("utf-8"))
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        while proc.poll() is None:
            if cancel_event is not None and cancel_event.is_set():
                self.stop()
                break
            time.sleep(0.05)
        with self._lock:
            stopped = self._stopped
            if self._proc is proc:
                self._proc = None
        return not stopped and proc.returncode == 0

    def stop(self) -> None:
        with self._lock:
            proc = self._proc
            if proc is not None and proc.poll() is None:
                self._stopped = True
                try:
                    proc.terminate()
                except OSError:
                    pass

    def is_speaking(self) -> bool:
        with self._lock:
            return self._proc is not None and self._proc.poll() is None
