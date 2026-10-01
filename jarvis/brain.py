"""The brain: Claude with tools in a streaming agent loop, plus the conversation so far.

One request (a "turn") can take many steps: Claude streams a reply, asks for tools, gets their
results and continues until it answers. Assistant replies are kept as the SDK's own objects,
because web search results must be sent back to the API exactly as they were received.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Callable

from . import prompts
from .claude_client import MissingKeyError, make_client
from .tools import Cancelled, ToolContext, ToolRegistry

log = logging.getLogger(__name__)

NO_KEY = ("I don't have an API key yet. Put your Anthropic key in the .env file in the Jarvis folder "
          "(ANTHROPIC_API_KEY=...) and restart me.")
STOPPED_NOTE = "(The user stopped this request before it finished.)"
STEP_LIMIT_NOTE = ("I've hit my step limit for one request, so I stopped here. "
                   "Say continue if you'd like me to keep going.")
SEARCH_ACTIVITY = "Searching the web…"
TextCallback = Callable[[str], None]


class BrainError(Exception):
    """A problem worth telling the user about, already phrased for them."""

    def __init__(self, message: str, kind: str = "error") -> None:
        super().__init__(message)
        self.kind = kind


@dataclass
class TurnResult:
    text: str
    steps: int = 0
    stop_reason: str | None = None


# ---------------------------------------------------------------- block helpers
# Assistant content holds SDK objects; user content holds plain dicts. These helpers read either.

def btype(block: Any) -> str | None:
    return block.get("type") if isinstance(block, dict) else getattr(block, "type", None)


def btext(block: Any) -> str:
    value = block.get("text") if isinstance(block, dict) else getattr(block, "text", "")
    return value or ""


def is_tool_result_message(msg: dict) -> bool:
    content = msg.get("content")
    return (msg.get("role") == "user" and isinstance(content, list) and bool(content)
            and all(btype(b) == "tool_result" for b in content))


def is_turn_start(msg: dict) -> bool:
    return msg.get("role") == "user" and not is_tool_result_message(msg)


def message_text(msg: dict) -> str:
    content = msg.get("content")
    if isinstance(content, str):
        return content
    return "".join(btext(b) for b in content or [] if btype(b) == "text")


def _strip_images_from_blocks(blocks: list) -> list:
    out = []
    for block in blocks:
        kind = btype(block)
        if kind == "image":
            out.append({"type": "text", "text": "[screenshot omitted from history]"})
        elif kind == "tool_result" and isinstance(block, dict) and isinstance(block.get("content"), list):
            new_block = dict(block)
            new_block["content"] = _strip_images_from_blocks(block["content"])
            out.append(new_block)
        else:
            out.append(block)
    return out


def strip_images(history: list[dict]) -> list[dict]:
    out = []
    for msg in history:
        if msg.get("role") == "user" and isinstance(msg.get("content"), list):
            msg = {"role": "user", "content": _strip_images_from_blocks(msg["content"])}
        out.append(msg)
    return out


def trim_history(history: list[dict], max_turns: int) -> list[dict]:
    starts = [i for i, m in enumerate(history) if is_turn_start(m)]
    if len(starts) > max_turns:
        history = history[starts[-max_turns]:]
    while history and not is_turn_start(history[0]):  # never start mid-turn
        history = history[1:]
    return history


def _as_dict(block: Any) -> dict:
    """An SDK content block as the same dict the SDK would send."""
    if isinstance(block, dict):
        return block
    data = block.model_dump(mode="json", exclude_unset=True)
    data.pop("parsed_output", None)
    return data


def _shorten(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit // 2] + "\n[... trimmed from history ...]\n" + value[-limit // 4:]


def compact_old_turns(history: list[dict], keep_turns: int = 2, limit: int = 2000) -> list[dict]:
    """Shorten long tool outputs and long tool inputs (like whole files) from older turns.

    The most recent turns stay intact; older ones keep their shape but lose the bulk, which keeps
    long sessions fast and cheap. Files can always be read again."""
    starts = [i for i, m in enumerate(history) if is_turn_start(m)]
    if len(starts) <= keep_turns:
        return history
    cutoff = starts[-keep_turns]
    out = []
    for i, msg in enumerate(history):
        if i < cutoff and is_tool_result_message(msg):
            blocks = []
            for block in msg["content"]:
                content = block.get("content")
                if isinstance(content, str) and len(content) > limit:
                    block = {**block, "content": _shorten(content, limit)}
                blocks.append(block)
            msg = {"role": "user", "content": blocks}
        elif i < cutoff and msg.get("role") == "assistant" and isinstance(msg.get("content"), list):
            blocks = []
            for block in msg["content"]:
                if btype(block) == "tool_use":
                    data = _as_dict(block)
                    args = data.get("input")
                    if isinstance(args, dict) and any(isinstance(v, str) and len(v) > limit for v in args.values()):
                        block = {**data, "input": {k: _shorten(v, limit) if isinstance(v, str) else v
                                                   for k, v in args.items()}}
                blocks.append(block)
            msg = {"role": "assistant", "content": blocks}
        out.append(msg)
    return out


def _error_text(e: Exception) -> str:
    body = getattr(e, "body", None)
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])
    return str(getattr(e, "message", "") or e)


class Brain:
    def __init__(self, cfg: Any, registry: ToolRegistry, memory: Any = None, client: Any = None) -> None:
        self.cfg = cfg
        self.registry = registry
        self.memory = memory
        self._client = client
        self.history: list[dict] = []
        self._lock = threading.RLock()
        self._active_stream: Any = None
        self._turn_memory = ""
        self.web_search = bool(cfg.llm.web_search)
        self.caching = bool(cfg.llm.prompt_caching)
        self.system_prompt = self._build_prompt()
        self.usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
                      "cache_creation_input_tokens": 0}

    # ------------------------------------------------------------ public

    @property
    def model(self) -> str:
        return str(self.cfg.llm.model)

    def reset(self) -> None:
        with self._lock:
            self.history = []

    def abort(self) -> None:
        """Called from another thread (Esc, Ctrl+C): close the open stream so the loop stops at once."""
        stream = self._active_stream
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass

    def usage_line(self) -> str:
        u = self.usage
        total_in = u["input_tokens"] + u["cache_read_input_tokens"] + u["cache_creation_input_tokens"]
        return (f"This session: {total_in:,} input tokens ({u['cache_read_input_tokens']:,} read from cache) "
                f"and {u['output_tokens']:,} output tokens.")

    def run_turn(self, user_content: list[dict] | str, ctx: ToolContext,
                 on_text: TextCallback | None = None, on_status: TextCallback | None = None,
                 on_activity: TextCallback | None = None) -> TurnResult:
        """Send one user request and let Claude work (tools included) until it answers."""
        with self._lock:
            if isinstance(user_content, str):
                user_content = [{"type": "text", "text": user_content}]
            turn: list[dict] = [{"role": "user", "content": list(user_content)}]
            base = list(self.history)
            # One memory snapshot per request keeps the system prompt (and its cache) stable.
            self._turn_memory = prompts.memory_block(self.memory)
            steps = 0
            stop_reason = None
            retried_overflow = False
            max_steps = int(self.cfg.llm.max_steps)
            try:
                while True:
                    ctx.check_cancel()
                    if steps >= max_steps:
                        turn = self._close_stopped_turn(turn, STEP_LIMIT_NOTE)
                        stop_reason = "step_limit"
                        break
                    steps += 1
                    try:
                        final = self._call(base + turn, ctx, on_text, on_activity)
                    except BrainError as e:
                        if e.kind == "overflow" and base and not retried_overflow:
                            log.warning("Context too long; dropping conversation history and retrying")
                            retried_overflow = True
                            base = []
                            self.history = []
                            continue
                        raise
                    self._add_usage(final)
                    content = list(final.content)
                    stop_reason = final.stop_reason
                    if stop_reason == "pause_turn":
                        # A server tool (web search) paused a long turn: send it back unchanged to continue.
                        if turn[-1]["role"] == "assistant":
                            turn[-1] = {"role": "assistant", "content": list(turn[-1]["content"]) + content}
                        else:
                            turn.append({"role": "assistant", "content": content})
                        continue
                    if turn[-1]["role"] == "assistant":  # continuation of a paused turn
                        content = list(turn[-1]["content"]) + content
                        turn[-1] = {"role": "assistant", "content": content}
                    else:
                        turn.append({"role": "assistant", "content": content})
                    tool_uses = [b for b in content if btype(b) == "tool_use"]
                    if not tool_uses:
                        break
                    results: list[dict] = []
                    turn.append({"role": "user", "content": results})
                    if stop_reason != "tool_use":
                        # e.g. max_tokens: the tool call may be cut off, so don't run it.
                        for block in tool_uses:
                            results.append({
                                "type": "tool_result", "tool_use_id": block.id, "is_error": True,
                                "content": ("Not executed: your reply hit the length limit before this call was "
                                            "complete. Split big content into smaller pieces (write_file, then "
                                            "write_file with append=true)."),
                            })
                        continue
                    self._run_tools(tool_uses, ctx, results, on_status)
            except (Cancelled, KeyboardInterrupt):
                self._commit(self._close_stopped_turn(turn))
                raise Cancelled()
            except BrainError as e:
                if len(turn) > 1:  # some work already happened: keep a valid record of it
                    self._commit(self._close_stopped_turn(turn, f"(This request failed: {e})"))
                raise
            self._commit(turn)
            text = STEP_LIMIT_NOTE if stop_reason == "step_limit" else self._final_text(turn, stop_reason)
            return TurnResult(text=text, steps=steps, stop_reason=stop_reason)

    # ------------------------------------------------------------ internals

    def _build_prompt(self) -> str:
        return prompts.build_system_prompt(self.cfg, self.registry.names(), self.web_search)

    def client(self) -> Any:
        if self._client is None:
            try:
                self._client = make_client(timeout=300.0, max_retries=2)
            except MissingKeyError as e:
                raise BrainError(NO_KEY, "auth") from e
            except Exception as e:  # pragma: no cover - depends on local setup
                raise BrainError(f"Couldn't start the Anthropic client: {e}") from e
        return self._client

    def _system(self) -> list[dict]:
        blocks = [{"type": "text", "text": self.system_prompt}]
        if self._turn_memory:
            blocks.append({"type": "text", "text": self._turn_memory})
        if self.caching:
            blocks[-1]["cache_control"] = {"type": "ephemeral"}
        return blocks

    def _tools(self) -> list[dict]:
        tools = list(self.registry.schemas())
        if self.web_search:
            tools.append({"type": "web_search_20250305", "name": "web_search",
                          "max_uses": int(self.cfg.llm.web_search_max_uses)})
        return tools

    def _call(self, messages: list[dict], ctx: ToolContext, on_text: TextCallback | None,
              on_activity: TextCallback | None) -> Any:
        import anthropic

        client = self.client()
        while True:
            kwargs: dict[str, Any] = {
                "model": self.model,
                "max_tokens": int(self.cfg.llm.max_tokens),
                "system": self._system(),
                "messages": messages,
                "tools": self._tools(),
            }
            if self.caching:
                kwargs["cache_control"] = {"type": "ephemeral"}
            try:
                return self._stream(client, kwargs, ctx, on_text, on_activity)
            except Cancelled:
                raise
            except Exception as e:
                if ctx.cancel.is_set():
                    raise Cancelled() from e
                if isinstance(e, anthropic.BadRequestError):
                    msg = _error_text(e)
                    low = msg.lower()
                    if self.web_search and ("web search" in low or "web_search" in low):
                        log.warning("Web search unavailable (%s); continuing without it", msg)
                        self.web_search = False
                        self.system_prompt = self._build_prompt()
                        continue
                    if self.caching and "cache" in low:
                        log.warning("Prompt caching rejected (%s); continuing without it", msg)
                        self.caching = False
                        continue
                raise self._friendly(e) from e

    def _stream(self, client: Any, kwargs: dict, ctx: ToolContext, on_text: TextCallback | None,
                on_activity: TextCallback | None) -> Any:
        with client.messages.stream(**kwargs) as stream:
            self._active_stream = stream
            try:
                for event in stream:
                    if ctx.cancel.is_set():
                        raise Cancelled()
                    kind = getattr(event, "type", None)
                    if kind == "text":
                        if on_text and event.text:
                            on_text(event.text)
                    elif kind == "content_block_start" and on_activity:
                        block = event.content_block
                        if block.type == "tool_use":
                            on_activity(self.registry.activity(block.name))
                        elif block.type == "server_tool_use":
                            on_activity(SEARCH_ACTIVITY)
                return stream.get_final_message()
            finally:
                self._active_stream = None

    def _friendly(self, e: Exception) -> BrainError:
        import anthropic

        msg = _error_text(e)
        if isinstance(e, TypeError) and "authentication" in msg.lower():
            return BrainError(NO_KEY, "auth")
        if isinstance(e, anthropic.AuthenticationError):
            return BrainError("My API key was rejected. Check ANTHROPIC_API_KEY in the .env file.", "auth")
        if isinstance(e, anthropic.PermissionDeniedError):
            return BrainError(f"This API key isn't allowed to do that: {msg}", "auth")
        if isinstance(e, anthropic.NotFoundError):
            return BrainError(f"The model {self.model} wasn't found. Check llm.model in config.local.yaml.", "model")
        if isinstance(e, anthropic.RateLimitError):
            return BrainError("I'm being rate limited by the API. Give it a minute and try again.", "rate")
        if isinstance(e, anthropic.BadRequestError):
            low = msg.lower()
            if "credit balance" in low:
                return BrainError("Your Anthropic credit balance is too low. Add credits in the Claude Console "
                                  "under Billing, then try again.", "billing")
            if "prompt is too long" in low or "too many tokens" in low or ("context" in low and "exceed" in low):
                return BrainError("This conversation got too long for me.", "overflow")
            return BrainError(f"The API rejected the request: {msg}", "request")
        overloaded = getattr(anthropic, "OverloadedError", None)
        if (overloaded and isinstance(e, overloaded)) or getattr(e, "status_code", None) == 529:
            return BrainError("Anthropic's servers are overloaded right now. Try again in a moment.", "server")
        if isinstance(e, anthropic.APIConnectionError):
            return BrainError("I can't reach the Anthropic API. Check the internet connection.", "network")
        if isinstance(e, anthropic.APIStatusError):
            status = getattr(e, "status_code", "?")
            if status == 413:
                return BrainError("That request was too large for the API.", "overflow")
            return BrainError(f"The API returned an error ({status}): {msg}", "server")
        log.exception("Unexpected error talking to the API")
        return BrainError(f"Something went wrong talking to the API: {msg}")

    def _run_tools(self, tool_uses: list, ctx: ToolContext, results: list[dict],
                   on_status: TextCallback | None) -> None:
        for block in tool_uses:
            ctx.check_cancel()
            args = block.input if isinstance(block.input, dict) else {}
            if on_status:
                on_status(self.registry.summarize(block.name, args))
            result = self.registry.execute(block.name, args, ctx)
            entry: dict[str, Any] = {"type": "tool_result", "tool_use_id": block.id,
                                     "content": result.to_content()}
            if result.is_error:
                entry["is_error"] = True
                log.info("Tool %s error: %s", block.name, result.text[:300])
            results.append(entry)

    def _close_stopped_turn(self, turn: list[dict], note: str = STOPPED_NOTE) -> list[dict]:
        """Make an unfinished turn valid history: every tool call answered, ending with a note."""
        turn = [dict(m) for m in turn]
        last = turn[-1]
        if len(turn) > 1 and last["role"] == "user" and isinstance(last.get("content"), list) and not last["content"]:
            turn.pop()  # the results message was created but no tool had finished yet
            last = turn[-1]
        if last["role"] == "assistant":
            pending = [b for b in last["content"] if btype(b) == "tool_use"]
            if pending:
                turn.append({"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": b.id, "is_error": True,
                     "content": "Cancelled by the user."} for b in pending]})
            elif any(btype(b) == "server_tool_use" for b in last["content"]):
                turn.pop()  # a paused web search can't be resumed later
        elif is_tool_result_message(last) and len(turn) >= 2 and turn[-2]["role"] == "assistant":
            answered = {b.get("tool_use_id") for b in last["content"]}
            missing = [{"type": "tool_result", "tool_use_id": b.id, "is_error": True,
                        "content": "Cancelled by the user."}
                       for b in turn[-2]["content"] if btype(b) == "tool_use" and b.id not in answered]
            last["content"] = list(last["content"]) + missing
        if turn[-1]["role"] == "assistant":
            turn[-1] = {"role": "assistant",
                        "content": list(turn[-1]["content"]) + [{"type": "text", "text": note}]}
        else:
            turn.append({"role": "assistant", "content": [{"type": "text", "text": note}]})
        return turn

    def _commit(self, turn: list[dict]) -> None:
        history = strip_images(self.history + turn)
        history = trim_history(history, int(self.cfg.llm.history_turns))
        self.history = compact_old_turns(history)

    @staticmethod
    def _final_text(turn: list[dict], stop_reason: str | None) -> str:
        last = next((m for m in reversed(turn) if m["role"] == "assistant"), None)
        text = message_text(last).strip() if last else ""
        if not text:
            if stop_reason == "refusal":
                return "I can't help with that one."
            return "Done."
        return text

    def _add_usage(self, message: Any) -> None:
        usage = getattr(message, "usage", None)
        if usage is None:
            return
        for key in self.usage:
            value = getattr(usage, key, None)
            if isinstance(value, int):
                self.usage[key] += value
