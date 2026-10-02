"""Terminal mode: chat with Jarvis by typing, or talk to it with --voice (replies are then spoken)."""
from __future__ import annotations

import importlib
import logging
import threading
import time
from typing import Any, Callable

from . import mac
from .assistant import Assistant
from .claude_client import get_api_key
from .config import ENV_PATH
from .ui import ConsoleUI
from .voice.tts import Speaker

log = logging.getLogger(__name__)

HELP = """Commands:
  /new             start a new conversation
  /memory          list what Jarvis remembers about you
  /forget <text>   forget memories that match (or a number from /memory)
  /usage           tokens used this session
  /help            show this help
  /quit            leave (Ctrl+D works too)
Ctrl+C stops Jarvis while it's listening, thinking or speaking.
With --voice: press Enter on an empty line, then speak. Say "never mind" to cancel,
"new conversation" to start fresh, or "repeat that" to hear the last answer again."""


def no_key_help() -> str:
    return (
        "Jarvis needs an Anthropic API key before it can chat.\n"
        "  1. Create one at https://platform.claude.com (Settings, then API keys).\n"
        f"  2. Run: open -e {ENV_PATH}\n"
        "  3. Paste the key after ANTHROPIC_API_KEY= and save, then run ./run.sh --cli again.\n"
        "Meanwhile you can test your microphone and speech recognition: ./run.sh --listen"
    )


class Chat:
    """A terminal session: the assistant engine plus the terminal it runs in."""

    def __init__(self, cfg, ui=None, brain=None, memory=None, recorder=None, stt=None,
                 speaker: Speaker | None = None) -> None:
        self.cfg = cfg
        self.ui = ui or ConsoleUI(name=cfg.assistant_name)
        self.assistant = Assistant(cfg, self.ui, brain=brain, memory=memory,
                                   speaker=speaker if speaker is not None else Speaker(cfg, enabled=False),
                                   recorder=recorder, transcriber=stt)
        self.assistant.host_app = mac.frontmost_app()  # the terminal we're running in: never type into it

    @property
    def brain(self):
        return self.assistant.brain

    @property
    def memory(self):
        return self.assistant.memory

    @property
    def host_app(self) -> dict | None:
        return self.assistant.host_app

    @property
    def stt(self):
        return self.assistant._get_stt()

    def ask(self, text: str, source: str = "cli") -> str | None:
        """Send one typed (or already transcribed) message. Returns the reply, or None if stopped or failed."""
        return self.assistant.process_text(text, source=source) or None

    def listen(self, follow_up: bool = False) -> str | None:
        """Listen for one spoken request, then answer it. Returns the reply, or None."""
        return self.assistant.listen_once(follow_up=follow_up) or None

    def stop(self) -> None:
        self.assistant.stop_now()


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


def run_interruptibly(chat: Chat, fn: Callable[..., Any], *args: Any) -> Any:
    """Run fn(*args) in a worker thread so Ctrl+C stops it cleanly (listening, thinking or speaking)."""
    box: dict = {}
    worker = threading.Thread(target=lambda: box.setdefault("result", fn(*args)), daemon=True)
    worker.start()
    while worker.is_alive():
        try:
            worker.join(0.1)
        except KeyboardInterrupt:
            chat.stop()
    return box.get("result")


def ask_interruptibly(chat: Chat, text: str, source: str = "cli") -> str | None:
    return run_interruptibly(chat, chat.ask, text, source)


def make_chat(cfg, voice: bool) -> Chat:
    return Chat(cfg, speaker=Speaker(cfg, enabled=voice and bool(cfg.voice.tts)))


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
    chat = chat or make_chat(cfg, voice)
    if voice and not load_speech(chat.stt):
        return 1
    print(f"{cfg.assistant_name} is ready (model {cfg.llm.model}). Type a message, or /help for commands.")
    if voice:
        replies = "spoken aloud" if chat.assistant.speaker.enabled else "shown as text (speech is off)"
        print(f"Voice is on: press Enter on an empty line, then speak. Replies are {replies}; Ctrl+C stops them.")
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
                run_interruptibly(chat, chat.listen)
                while chat.assistant.follow_up_pending:  # Jarvis asked a question: listen for the answer
                    run_interruptibly(chat, chat.listen, True)
            continue
        if line.startswith("/"):
            if handle_command(chat, line) == "quit":
                return 0
            continue
        ask_interruptibly(chat, line)


def run_once(cfg, text: str, chat: Chat | None = None, voice: bool = False) -> int:
    if chat is None and not get_api_key():
        print(no_key_help())
        return 1
    chat = chat or make_chat(cfg, voice)
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
