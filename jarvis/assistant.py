"""The assistant: turns hotkeys and speech into requests, runs them, and speaks the answers.

States: idle, listening, transcribing, thinking, speaking, waiting (dialog open), error.
Hotkey handlers run on the UI thread and only flip state or queue jobs; the real work happens on a
single worker thread so requests never overlap.
"""
from __future__ import annotations

import logging
import queue
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from . import mac, prompts
from .brain import Brain, BrainError
from .memory import Memory
from .tools import Cancelled, ToolContext, build_registry
from .voice.recorder import MicrophoneError
from .voice.tts import Speaker

log = logging.getLogger(__name__)

IDLE_STATES = ("idle", "error")

RESET_PHRASES = {"new conversation", "start over", "start a new conversation", "reset", "reset the conversation",
                 "clear history", "clear the conversation", "forget this conversation", "new chat", "new topic"}
NEVERMIND_PHRASES = {"never mind", "nevermind", "cancel", "stop", "forget it", "nothing", "no thanks",
                     "nothing thanks", "stop listening", "go away", "shut up", "be quiet", "quiet"}
REPEAT_PHRASES = {"repeat that", "say that again", "repeat", "what did you say", "come again", "pardon"}
THANKS_PHRASES = {"thank you", "thanks", "thanks a lot", "thank you so much", "cheers", "great thanks",
                  "perfect thanks", "awesome thanks"}


@dataclass
class Job:
    kind: str = "listen"          # listen | type | text
    text: str = ""
    tutor: bool = False
    source: str = "voice"         # voice | wake | typed | cli
    frontmost: dict | None = None
    follow_up: bool = False
    ptt: bool = False
    stop: threading.Event = field(default_factory=threading.Event)


class Assistant:
    def __init__(self, cfg: Any, ui: Any, *, brain: Brain | None = None, registry: Any = None,
                 memory: Memory | None = None, speaker: Speaker | None = None, recorder: Any = None,
                 transcriber: Any = None) -> None:
        self.cfg = cfg
        self.ui = ui
        self.memory = memory or Memory(cfg.paths.memory_file)
        self.registry = registry or build_registry(cfg)
        self.brain = brain or Brain(cfg, self.registry, self.memory)
        self.speaker = speaker if speaker is not None else Speaker(cfg)
        self._recorder = recorder
        self._stt = transcriber
        self.state = "idle"
        self.cancel = threading.Event()
        self.jobs: queue.Queue = queue.Queue()
        self._listen_queued = threading.Event()
        self.current_listen: Job | None = None
        self._ptt_job: Job | None = None
        self.last_response = ""
        self.wakeword: Any = None
        self.sync_mode = False
        self._worker: threading.Thread | None = None
        self._last_escape = 0.0
        self.host_app: dict | None = None   # terminal mode: the terminal running Jarvis (never type into it)
        self.follow_up_pending = False      # terminal mode: the last spoken reply asked the user a question

    # ------------------------------------------------------------ lifecycle

    def start(self, preload: bool = True) -> None:
        self._worker = threading.Thread(target=self._work_loop, name="jarvis-worker", daemon=True)
        self._worker.start()
        if preload:
            threading.Thread(target=self._preload, name="jarvis-preload", daemon=True).start()
        if self.cfg.wake_word.enabled:
            try:
                from .voice.wakeword import WakeWordListener
            except ImportError:
                self.ui.hint("The wake word files are missing; reinstall Jarvis to use it. The hotkey still works.")
            else:
                self.wakeword = WakeWordListener(self.cfg, on_wake=self.on_wake, on_error=self.ui.hint)
                self.wakeword.start()

    def shutdown(self) -> None:
        self.cancel.set()
        self.speaker.stop()
        self.brain.abort()
        if self.wakeword is not None:
            self.wakeword.stop()
        self.jobs.put(None)

    def _preload(self) -> None:
        try:
            self._get_recorder().warmup()
        except Exception as e:
            log.warning("Microphone warm-up failed: %s", e)
        try:
            stt = self._get_stt()
            if not stt.is_cached():
                self.ui.hint("Downloading the speech model (one time only, a few hundred MB)…")
            stt.load()
            log.info("Speech model ready")
        except Exception as e:
            log.exception("Speech model failed to load")
            self.ui.show_error(f"The speech model failed to load: {e}")

    def _get_recorder(self) -> Any:
        if self._recorder is None:
            from .voice.recorder import Recorder

            self._recorder = Recorder(self.cfg)
        return self._recorder

    def _get_stt(self) -> Any:
        if self._stt is None:
            from .voice.stt import Transcriber

            self._stt = Transcriber(self.cfg)
        return self._stt

    # ------------------------------------------------------------ triggers (UI thread)

    def on_talk(self, tutor: bool = False) -> None:
        state = self.state
        if state == "listening":
            job = self.current_listen
            if job is not None:
                job.stop.set()       # second press: done talking
            return
        if state == "speaking":
            self.speaker.stop()      # barge in
            self._enqueue_listen(tutor=tutor)
            return
        if state in IDLE_STATES:
            self._enqueue_listen(tutor=tutor)
            return
        if state == "waiting":
            self.ui.hint("Answer the open dialog first.")
        else:
            self.ui.hint("Still working on the last request. Press Esc twice to stop it.")

    def on_type(self, tutor: bool = False) -> None:
        if self.state == "speaking":
            self.speaker.stop()
        elif self.state not in IDLE_STATES:
            self.ui.hint("Still working on the last request. Press Esc twice to stop it.")
            return
        frontmost = self._frontmost()
        self._set_state("waiting", "Type your request", "A text box is open.")
        self.jobs.put(Job("type", tutor=tutor, source="typed", frontmost=frontmost))

    def on_ptt_down(self) -> None:
        if self.state == "speaking":
            self.speaker.stop()
        elif self.state not in IDLE_STATES:
            return
        self._ptt_job = self._enqueue_listen(ptt=True)

    def on_ptt_up(self) -> None:
        job, self._ptt_job = self._ptt_job, None
        if job is not None:
            job.stop.set()

    def on_wake(self) -> None:
        if self.state in IDLE_STATES:
            self._enqueue_listen(source="wake")

    def on_cancel(self) -> None:
        """Esc. One press stops listening or speaking; working on a task needs a second press within
        1.2 s so a stray Esc in another app doesn't kill a long job."""
        state = self.state
        if state in IDLE_STATES or state == "waiting":
            return
        if state in ("thinking", "transcribing"):
            now = time.monotonic()
            if now - self._last_escape > 1.2:
                self._last_escape = now
                self.ui.hint("Press Esc again to stop.")
                return
        self._last_escape = 0.0
        self.stop_now()

    def stop_now(self) -> None:
        """Stop whatever is happening right away (Ctrl+C in the terminal, the second Esc in the app)."""
        self.cancel.set()
        job = self.current_listen
        if job is not None:
            job.stop.set()
        self.speaker.stop()
        self.brain.abort()
        self._drain_jobs()
        self.ui.hide_pointer()

    def new_conversation(self) -> None:
        self.brain.reset()
        self.ui.hint("Started a new conversation.")

    # ------------------------------------------------------------ synchronous use (CLI)

    def process_text(self, text: str, tutor: bool = False, source: str = "cli") -> str:
        self.sync_mode = True
        job = Job("text", text=text, tutor=tutor, source=source, frontmost=self._frontmost())
        return self._run_job(job) or ""

    def listen_once(self, tutor: bool = False, follow_up: bool = False) -> str:
        self.sync_mode = True
        job = Job("listen", tutor=tutor, source="voice", follow_up=follow_up, frontmost=self._frontmost())
        self.current_listen = job
        return self._run_job(job) or ""

    # ------------------------------------------------------------ worker

    def _enqueue_listen(self, tutor: bool = False, ptt: bool = False, source: str = "voice",
                        follow_up: bool = False, frontmost: dict | None = None) -> Job | None:
        if self._listen_queued.is_set():
            return None
        self._listen_queued.set()
        job = Job("listen", tutor=tutor, ptt=ptt, source=source, follow_up=follow_up,
                  frontmost=frontmost if frontmost is not None else self._frontmost())
        self.current_listen = job
        self._set_state("listening", subtitle=self._listen_hint(job))
        self.jobs.put(job)
        return job

    def _drain_jobs(self) -> None:
        while True:
            try:
                self.jobs.get_nowait()
            except queue.Empty:
                break
        self._listen_queued.clear()

    def _work_loop(self) -> None:
        while True:
            job = self.jobs.get()
            if job is None:
                return
            if job.kind == "listen":
                self._listen_queued.clear()
            self._run_job(job)
            if self.jobs.empty():
                if self.state not in IDLE_STATES:
                    self._set_state("idle")
                if self.wakeword is not None:
                    self.wakeword.resume()

    def _run_job(self, job: Job) -> str | None:
        self.cancel.clear()
        self.follow_up_pending = False
        try:
            if job.kind == "listen":
                return self._handle_listen(job)
            if job.kind == "type":
                return self._handle_type(job)
            return self._handle_text(job.text, job)
        except (Cancelled, KeyboardInterrupt):
            self.speaker.stop()
            self.ui.show_status("Stopped.")
            self._set_state("idle", "Stopped", "")
            self.ui.schedule_hide(3)
        except MicrophoneError as e:
            self._error(str(e))
        except BrainError as e:
            self._error(str(e))
        except Exception as e:
            log.exception("Request failed")
            self._error(f"Something went wrong: {e}")
        finally:
            if self.current_listen is job:
                self.current_listen = None
        return None

    def _handle_listen(self, job: Job) -> str | None:
        if self.wakeword is not None:
            self.wakeword.pause()
        self.current_listen = job
        self._set_state("listening", subtitle=self._listen_hint(job))
        if self.cfg.voice.sounds:
            mac.play_sound("Tink")
        audio = self._get_recorder().record(
            stop_event=job.stop, abort_event=self.cancel, on_level=self.ui.set_level,
            silence_stop=not job.ptt,
            start_timeout=6.0 if job.follow_up else None,
        )
        if self.cancel.is_set():
            raise Cancelled()
        if audio is None:
            if job.follow_up:
                self._set_state("idle")
                self.ui.schedule_hide()
            else:
                self._set_state("idle", "Didn't hear anything", self._talk_hint())
                self.ui.schedule_hide(4)
            return None
        if self.cfg.voice.sounds:
            mac.play_sound("Pop")
        stt = self._get_stt()
        self._set_state("transcribing", subtitle="Turning speech into text…" if stt.loaded
                        else "Loading the speech model (first time takes a moment)…")
        text = stt.transcribe(audio)
        if self.cancel.is_set():
            raise Cancelled()
        if not text:
            self._set_state("idle", "Didn't catch that", "Try again a little closer to the mic.")
            self.ui.schedule_hide(4)
            return None
        return self._handle_text(text, job)

    def _handle_type(self, job: Job) -> str | None:
        prompt = "What should I help with on your screen?" if job.tutor else "What should I do?"
        text = self.ui.ask_text(prompt)
        if not text or not text.strip():
            self._set_state("idle")
            self.ui.schedule_hide(2)
            return None
        return self._handle_text(text.strip(), job)

    def _handle_text(self, text: str, job: Job) -> str:
        text = text.strip()
        self.ui.begin_turn()
        if job.source != "cli":  # typed into the terminal: no need to echo it back
            self.ui.show_user_text(text)
        local = self._local_command(text)
        if local is not None:
            if local:
                self.ui.show_response(local)
                self._speak_final(local, job)
            else:
                self._set_state("idle", "Okay", "")
                self.ui.schedule_hide(2)
            return local

        self._set_state("thinking", subtitle=f"“{text}”")
        ctx = ToolContext(cfg=self.cfg, ui=self.ui, memory=self.memory, cancel=self.cancel,
                          frontmost=job.frontmost, host_app=self.host_app,
                          speak=self._speak_inline if self.speaker.enabled else None)
        content: list[dict] = []
        shot = self._capture_for_tutor() if job.tutor else None
        if shot is not None:
            ctx.shot = shot
            content.append(shot.image_block())
        context = prompts.turn_context(self.cfg, frontmost=job.frontmost, source=job.source,
                                       tutor=job.tutor, shot=shot, speak=self.speaker.enabled)
        content.append({"type": "text", "text": f"{context}\n\n{text}"})
        result = self.brain.run_turn(content, ctx, on_text=self.ui.append_text,
                                     on_status=self.ui.show_status, on_activity=self.ui.set_activity)
        reply = result.text
        self.last_response = reply
        self.ui.show_response(reply)
        if ctx.shot is not None:
            self.ui.hide_pointer_after(float(self.cfg.ui.pointer_hide_seconds))
        self._speak_final(reply, job)
        return reply

    def _speak_final(self, reply: str, job: Job) -> None:
        completed = True
        if self.speaker.enabled and reply:
            self._set_state("speaking")
            completed = self.speaker.speak(reply, cancel_event=self.cancel)
        if self.cancel.is_set():
            raise Cancelled()
        if self._listen_queued.is_set():
            return  # the user barged in with a new request
        follow_up = (completed and self.cfg.voice.follow_up_listen and job.source in ("voice", "wake")
                     and reply.rstrip().endswith("?"))
        if self.sync_mode:  # terminal mode: the chat loop listens for the answer
            self.follow_up_pending = follow_up
            self._set_state("idle")
            return
        if follow_up and self.jobs.empty():
            self._enqueue_listen(tutor=job.tutor, follow_up=True, source=job.source, frontmost=job.frontmost)
            return
        self._set_state("idle")
        self.ui.schedule_hide()

    def _speak_inline(self, text: str) -> None:
        """Blocking speech in the middle of a task (tutor walkthroughs, progress updates)."""
        if self.cancel.is_set():
            raise Cancelled()
        if self.speaker.enabled:
            self.speaker.speak(text, cancel_event=self.cancel)
        else:  # give the user time to read the label
            end = time.monotonic() + min(4.0, 0.8 + 0.05 * len(text))
            while time.monotonic() < end and not self.cancel.is_set():
                time.sleep(0.05)
        if self.cancel.is_set():
            raise Cancelled()

    # ------------------------------------------------------------ helpers

    def _capture_for_tutor(self) -> Any:
        if sys.platform != "darwin":
            return None
        try:
            allowed = mac.screen_recording_allowed()
            if allowed is False:
                mac.screen_recording_allowed(request=True)
                self.ui.show_status("Screen Recording is off for your terminal app, so I may only see the desktop. "
                                    "Turn it on in System Settings > Privacy & Security, then restart the terminal.")
            with self.ui.hidden_for_capture():
                return mac.capture_screen()
        except Exception as e:
            log.warning("Screenshot failed: %s", e)
            self.ui.show_status(f"Couldn't take a screenshot: {e}")
            return None

    def _local_command(self, text: str) -> str | None:
        """Tiny commands handled without calling Claude. Returns a reply, "" to stay quiet, or None."""
        norm = re.sub(r"[^a-z' ]", " ", text.lower())
        norm = re.sub(r"\s+", " ", norm).strip()
        name = str(self.cfg.assistant_name).lower()
        norm = re.sub(rf"^(?:(?:hey|hi|hello|ok|okay|yo)\s+)?{re.escape(name)}\b\s*", "", norm).strip()
        norm = re.sub(rf"\s*\b(?:please|{re.escape(name)})$", "", norm).strip()
        if not norm:
            return f"Yes, {self.cfg.user_name}?"
        if norm in RESET_PHRASES:
            self.brain.reset()
            return "Fresh start."
        if norm in NEVERMIND_PHRASES:
            return ""
        if norm in REPEAT_PHRASES:
            return self.last_response or "I haven't said anything yet."
        if norm in THANKS_PHRASES:
            return "Any time."
        return None

    def _listen_hint(self, job: Job) -> str:
        if job.ptt:
            return "Release the key when you're done."
        if job.follow_up:
            return "Go ahead, I'm listening."
        if job.tutor:
            return "Ask about anything on your screen."
        return "Speak now. I'll stop when you pause."

    def _talk_hint(self) -> str:
        return "Press Enter and start talking." if self.sync_mode else "Press the hotkey and start talking."

    def _frontmost(self) -> dict | None:
        if sys.platform != "darwin" or self.host_app:  # in the terminal, "the app you were in" is the terminal
            return None
        try:
            return mac.frontmost_app()
        except Exception:
            return None

    def _set_state(self, state: str, title: str | None = None, subtitle: str | None = None) -> None:
        self.state = state
        try:
            self.ui.set_state(state, title, subtitle)
        except Exception:
            log.exception("UI set_state failed")

    def _error(self, message: str) -> None:
        log.info("Error shown to user: %s", message)  # already on screen; keep it in the log only
        self._set_state("error", "Something went wrong", message)
        self.ui.show_error(message)
        try:
            self.speaker.speak(message.split(". ")[0] + ".", cancel_event=self.cancel)
        except Exception:
            pass
        self.ui.schedule_hide(12)
