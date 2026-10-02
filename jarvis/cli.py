"""Terminal chat: type to Jarvis and read its replies as they stream in."""
from __future__ import annotations

import importlib
import logging
import threading
import time

from . import mac, prompts
from .brain import SEARCH_ACTIVITY, Brain, BrainError
from .claude_client import get_api_key
from .config import ENV_PATH
from .memory import Memory
from .tools import Cancelled, ToolContext, build_registry
from .ui import ConsoleUI

log = logging.getLogger(__name__)

HELP = """Commands:
  /new             start a new conversation
  /memory          list what Jarvis remembers about you
  /forget <text>   forget memories that match (or a number from /memory)
  /usage           tokens used this session
  /help            show this help
  /quit            leave (Ctrl+D works too)
Press Ctrl+C while Jarvis is answering to stop it.
With --voice: press Enter on an empty line, then speak."""


def no_key_help() -> str:
    return (
        "Jarvis needs an Anthropic API key before it can chat.\n"
        "  1. Create one at https://platform.claude.com (Settings, then API keys).\n"
        f"  2. Run: open -e {ENV_PATH}\n"
        "  3. Paste the key after ANTHROPIC_API_KEY= and save, then run ./run.sh --cli again.\n"
        "Meanwhile you can test your microphone and speech recognition: ./run.sh --listen"
    )


class Chat:
    """One terminal conversation with the brain (voice arrives in phases 5 and 6)."""

    def __init__(self, cfg, ui=None, brain: Brain | None = None, memory: Memory | None = None,
                 recorder=None, stt=None) -> None:
        self.cfg = cfg
        self.ui = ui or ConsoleUI(name=cfg.assistant_name)
        self.memory = memory if memory is not None else Memory(cfg.paths.memory_file)
        self.brain = brain or Brain(cfg, build_registry(cfg), self.memory)
        self.cancel = threading.Event()
        self.host_app = mac.frontmost_app()  # the terminal we're running in: never type into it
        self._recorder = recorder
        self._stt = stt

    @property
    def recorder(self):
        if self._recorder is None:
            from .voice.recorder import Recorder

            self._recorder = Recorder(self.cfg)
        return self._recorder

    @property
    def stt(self):
        if self._stt is None:
            from .voice.stt import Transcriber

            self._stt = Transcriber(self.cfg)
        return self._stt

    def listen(self) -> str | None:
        """Record one spoken request and return the words (None if nothing usable was heard)."""
        return hear(self.cfg, self.recorder, self.stt, self.ui)

    def ask(self, text: str, source: str = "cli") -> str | None:
        """Send one message and stream the answer. Returns the reply, or None if stopped or failed."""
        self.cancel.clear()
        ctx = ToolContext(cfg=self.cfg, ui=self.ui, memory=self.memory, cancel=self.cancel,
                          host_app=self.host_app)
        content = prompts.turn_context(self.cfg, source=source, speak=False) + "\n\n" + text
        self.ui.begin_turn()
        try:
            result = self.brain.run_turn(content, ctx, on_text=self.ui.append_text,
                                         on_status=self.ui.show_status, on_activity=self._activity)
        except Cancelled:
            self.ui.hint("stopped")
            return None
        except BrainError as e:
            self.ui.show_error(str(e))
            return None
        self.ui.show_response(result.text)
        return result.text

    def stop(self) -> None:
        self.cancel.set()
        self.brain.abort()

    def _activity(self, text: str) -> None:
        if text == SEARCH_ACTIVITY:  # tool calls get their own status line when they run
            self.ui.show_status(text)


def hear(cfg, recorder, stt, ui) -> str | None:
    """Listen for one spoken request in the terminal and return the words (None if nothing usable)."""
    from .voice.recorder import MicrophoneError

    stop, abort = threading.Event(), threading.Event()
    if cfg.voice.sounds:
        mac.play_sound("Tink")
    ui.set_state("listening")
    try:
        audio = recorder.record(stop, abort, on_level=ui.set_level)
    except KeyboardInterrupt:
        abort.set()
        ui.hint("stopped listening")
        return None
    except MicrophoneError as e:
        ui.show_error(str(e))
        return None
    if audio is None:
        ui.hint("didn't hear anything; press Enter to try again")
        return None
    if cfg.voice.sounds:
        mac.play_sound("Pop")
    ui.set_state("transcribing")
    started = time.monotonic()
    try:
        text = stt.transcribe(audio)
    except KeyboardInterrupt:
        ui.hint("stopped")
        return None
    except Exception as e:
        log.exception("Speech recognition failed")
        ui.show_error(f"Speech recognition failed: {e}")
        return None
    if not text:
        ui.hint("couldn't make out any words; try again a little closer to the microphone")
        return None
    log.info("Transcribed %.1fs of audio in %.1fs", len(audio) / 16_000, time.monotonic() - started)
    ui.show_user_text(text)
    return text


def load_speech(stt) -> bool:
    """Load the speech model (downloading it the first time), saying what's happening."""
    if stt.loaded:
        return True
    if stt.is_cached():
        print(f"Loading the speech model ({stt.model_name})...")
    else:
        print(f"Downloading the speech model {stt.model_name} ({stt.download_size}). This happens only once.")
    try:
        stt.load()
    except KeyboardInterrupt:
        print("Stopped.")
        return False
    except Exception as e:
        log.exception("Couldn't load the speech model")
        print(f"Couldn't load the speech model: {e}\n"
              "The first download needs an internet connection; check it and try again.")
        return False
    return True


def ask_interruptibly(chat: Chat, text: str, source: str = "cli") -> str | None:
    """Run one request in a worker thread so Ctrl+C can stop it cleanly."""
    box: dict = {}
    worker = threading.Thread(target=lambda: box.setdefault("reply", chat.ask(text, source)), daemon=True)
    worker.start()
    while worker.is_alive():
        try:
            worker.join(0.1)
        except KeyboardInterrupt:
            chat.stop()
    return box.get("reply")


def handle_command(chat: Chat, line: str) -> str | None:
    """Run a /command. Returns "quit" when the user wants to leave."""
    command, _, arg = line.partition(" ")
    command, arg = command.lower(), arg.strip()
    if command in ("/quit", "/exit", "/q"):
        return "quit"
    if command == "/new":
        chat.brain.reset()
        print("Started a new conversation.")
    elif command == "/memory":
        facts = chat.memory.list()
        print("\n".join(f"{i}. {f}" for i, f in enumerate(facts, 1)) if facts else "Nothing saved yet.")
    elif command == "/forget":
        if not arg:
            print("Usage: /forget <text or number>")
        else:
            removed = chat.memory.remove(arg)
            print(("Forgot: " + "; ".join(removed)) if removed else "Nothing matched.")
    elif command == "/usage":
        print(chat.brain.usage_line())
    elif command == "/help":
        print(HELP)
    else:
        print(f"Unknown command {command}. Type /help for the list.")
    return None


def run_cli(cfg, chat: Chat | None = None, voice: bool = False) -> int:
    if chat is None and not get_api_key():
        print(no_key_help())
        return 1
    try:
        importlib.import_module("readline")  # arrow keys and history while typing
    except ImportError:
        pass
    chat = chat or Chat(cfg)
    if voice and not load_speech(chat.stt):
        return 1
    print(f"{cfg.assistant_name} is ready (model {cfg.llm.model}). Type a message, or /help for commands.")
    if voice:
        print("Voice is on: press Enter on an empty line, then speak. Replies are text until phase 6.")
    for warning in getattr(cfg, "warnings", []):
        print(f"Warning: {warning}")
    prompt = "\nyou (Enter to talk) › " if voice else "\nyou › "
    while True:
        try:
            line = input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line:
            if voice:
                heard = chat.listen()
                if heard:
                    ask_interruptibly(chat, heard, source="voice")
            continue
        if line.startswith("/"):
            if handle_command(chat, line) == "quit":
                return 0
            continue
        ask_interruptibly(chat, line)


def run_once(cfg, text: str, chat: Chat | None = None) -> int:
    if chat is None and not get_api_key():
        print(no_key_help())
        return 1
    chat = chat or Chat(cfg)
    return 0 if ask_interruptibly(chat, text) is not None else 1


def run_listen(cfg, recorder=None, stt=None, ui=None) -> int:
    """Microphone and speech-recognition test. No API key needed: everything runs on this Mac."""
    from .voice.recorder import Recorder
    from .voice.stt import Transcriber

    ui = ui or ConsoleUI(name=cfg.assistant_name)
    stt = stt or Transcriber(cfg)
    recorder = recorder or Recorder(cfg)
    print("Microphone test: press Enter, say something, then pause. Your words appear below.")
    print("Everything runs on this Mac, so no API key needed. Ctrl+C or Ctrl+D quits.")
    if not load_speech(stt):
        return 1
    try:
        importlib.import_module("readline")
    except ImportError:
        pass
    while True:
        try:
            input("\nPress Enter to talk › ")
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        hear(cfg, recorder, stt, ui)
