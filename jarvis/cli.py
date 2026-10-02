"""Terminal chat: type to Jarvis and read its replies as they stream in."""
from __future__ import annotations

import importlib
import logging
import threading

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
Press Ctrl+C while Jarvis is answering to stop it."""


def no_key_help() -> str:
    return (
        "Jarvis needs an Anthropic API key before it can chat.\n"
        "  1. Create one at https://platform.claude.com (Settings, then API keys).\n"
        f"  2. Run: open -e {ENV_PATH}\n"
        "  3. Paste the key after ANTHROPIC_API_KEY= and save, then run ./run.sh --cli again."
    )


class Chat:
    """One terminal conversation with the brain (voice arrives in phases 5 and 6)."""

    def __init__(self, cfg, ui=None, brain: Brain | None = None, memory: Memory | None = None) -> None:
        self.cfg = cfg
        self.ui = ui or ConsoleUI(name=cfg.assistant_name)
        self.memory = memory if memory is not None else Memory(cfg.paths.memory_file)
        self.brain = brain or Brain(cfg, build_registry(cfg), self.memory)
        self.cancel = threading.Event()
        self.host_app = mac.frontmost_app()  # the terminal we're running in: never type into it

    def ask(self, text: str) -> str | None:
        """Send one message and stream the answer. Returns the reply, or None if stopped or failed."""
        self.cancel.clear()
        ctx = ToolContext(cfg=self.cfg, ui=self.ui, memory=self.memory, cancel=self.cancel,
                          host_app=self.host_app)
        content = prompts.turn_context(self.cfg, source="cli") + "\n\n" + text
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


def ask_interruptibly(chat: Chat, text: str) -> str | None:
    """Run one request in a worker thread so Ctrl+C can stop it cleanly."""
    box: dict = {}
    worker = threading.Thread(target=lambda: box.setdefault("reply", chat.ask(text)), daemon=True)
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


def run_cli(cfg, chat: Chat | None = None) -> int:
    if chat is None and not get_api_key():
        print(no_key_help())
        return 1
    try:
        importlib.import_module("readline")  # arrow keys and history while typing
    except ImportError:
        pass
    chat = chat or Chat(cfg)
    print(f"{cfg.assistant_name} is ready (model {cfg.llm.model}). Type a message, or /help for commands.")
    for warning in getattr(cfg, "warnings", []):
        print(f"Warning: {warning}")
    while True:
        try:
            line = input("\nyou › ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line:
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
