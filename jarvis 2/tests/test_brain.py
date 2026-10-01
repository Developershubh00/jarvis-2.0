"""Phase 2 tests: the brain's agent loop, memory, the tool registry, prompts and terminal chat.

The Claude API is simulated at the HTTP level with the same streaming format the real API uses,
so these run without an API key or network.  Run:  python -m unittest discover -s tests -v
"""
from __future__ import annotations

import io
import json
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import anthropic  # noqa: E402
import httpx2  # noqa: E402

from jarvis import cli  # noqa: E402
from jarvis import config as config_module  # noqa: E402
from jarvis.brain import (NO_KEY, STEP_LIMIT_NOTE, STOPPED_NOTE, Brain, BrainError,  # noqa: E402
                          compact_old_turns, is_turn_start, strip_images, trim_history)
from jarvis.config import load_config  # noqa: E402
from jarvis.memory import Memory  # noqa: E402
from jarvis.prompts import build_system_prompt, turn_context  # noqa: E402
from jarvis.tools import (Cancelled, ToolContext, ToolError, ToolRegistry, ToolResult,  # noqa: E402
                          build_registry, truncate)
from jarvis.ui import ConsoleUI  # noqa: E402

NOWHERE = Path("/nonexistent/jarvis-test.yaml")


# ---------------------------------------------------------------- simulated Claude API

def sse(events: list[dict]) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def reply(blocks: list[dict], stop_reason: str = "end_turn") -> bytes:
    """A streamed Messages API response containing ``blocks``."""
    events: list[dict] = [{"type": "message_start", "message": {
        "id": "msg_test", "type": "message", "role": "assistant", "model": "test-model", "content": [],
        "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 1}}}]
    for i, block in enumerate(blocks):
        if block["type"] == "text":
            events.append({"type": "content_block_start", "index": i, "content_block": {"type": "text", "text": ""}})
            words = block["text"].split(" ")
            for n, word in enumerate(words):
                piece = word if n == len(words) - 1 else word + " "
                events.append({"type": "content_block_delta", "index": i,
                               "delta": {"type": "text_delta", "text": piece}})
        elif block["type"] in ("tool_use", "server_tool_use"):
            events.append({"type": "content_block_start", "index": i, "content_block": {
                "type": block["type"], "id": block["id"], "name": block["name"], "input": {}}})
            events.append({"type": "content_block_delta", "index": i, "delta": {
                "type": "input_json_delta", "partial_json": json.dumps(block["input"])}})
        else:
            events.append({"type": "content_block_start", "index": i, "content_block": block})
        events.append({"type": "content_block_stop", "index": i})
    events.append({"type": "message_delta", "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                   "usage": {"output_tokens": 12}})
    events.append({"type": "message_stop"})
    return sse(events)


def api_error(status: int, kind: str, message: str) -> tuple:
    return status, {"type": "error", "error": {"type": kind, "message": message}}


class FakeAPI:
    """Answers HTTP requests from a script of responses and records what was sent."""

    def __init__(self, responses: list) -> None:
        self.responses = list(responses)
        self.requests: list[dict] = []

    def __call__(self, request):
        request.read()
        self.requests.append(json.loads(request.content))
        item = self.responses.pop(0)
        if isinstance(item, tuple):
            status, payload = item
            return httpx2.Response(status, json=payload)
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=item)

    def client(self):
        return anthropic.Anthropic(api_key="test-key", max_retries=0,
                                   http_client=httpx2.Client(transport=httpx2.MockTransport(self)))


def text(s: str) -> dict:
    return {"type": "text", "text": s}


def tool(id_: str, name: str, args: dict) -> dict:
    return {"type": "tool_use", "id": id_, "name": name, "input": args}


SEARCH = [
    {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search", "input": {"query": "news"}},
    {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_1", "content": [
        {"type": "web_search_result", "url": "https://example.com", "title": "Example",
         "encrypted_content": "ENC-123", "page_age": None}]},
]


class Base(unittest.TestCase):
    """Isolated from your real .env, settings and data folder."""

    def setUp(self):
        logging.disable(logging.CRITICAL)  # expected warnings would only clutter the test output
        self.addCleanup(logging.disable, logging.NOTSET)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for patcher in (mock.patch.dict(os.environ), mock.patch.object(config_module, "ENV_PATH", NOWHERE)):
            patcher.start()
            self.addCleanup(patcher.stop)
        os.environ.pop("JARVIS_MODEL", None)
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-key-for-unit-tests"
        self.cfg = self.make_cfg()

    def make_cfg(self, **overrides):
        base = {"paths": {"workspace": str(Path(self.tmp.name) / "ws")}}
        for key, value in overrides.items():
            base.setdefault(key, {}).update(value)
        return load_config(NOWHERE, overrides=base, data_dir=Path(self.tmp.name) / "data", local_path=NOWHERE)


class BrainTests(Base):
    def setUp(self):
        super().setUp()
        self.ui = ConsoleUI(stream=io.StringIO(), interactive=False, confirm_answer=True, color=False)
        self.reg = ToolRegistry()
        self.calls: list[dict] = []

        def echo(ctx, a):
            self.calls.append(a)
            return f"echo: {a.get('msg')}"

        self.reg.add("echo", "Echo a message", {"msg": {"type": "string"}}, ("msg",), func=echo)

    def make(self, responses, cfg=None):
        api = FakeAPI(responses)
        cfg = cfg or self.cfg
        brain = Brain(cfg, self.reg, Memory(Path(self.tmp.name) / "m.json"), client=api.client())
        return api, brain, ToolContext(cfg=cfg, ui=self.ui, memory=brain.memory)

    def assert_valid_history(self, history):
        """The API's rules: alternate roles, start with the user, answer every tool call."""
        self.assertEqual(history[0]["role"], "user")
        for a, b in zip(history, history[1:]):
            self.assertNotEqual(a["role"], b["role"])
        for i, msg in enumerate(history):
            if msg["role"] == "assistant":
                calls = [getattr(b, "id", None) or b.get("id") for b in msg["content"]
                         if (getattr(b, "type", None) or b.get("type")) == "tool_use"]
                if calls:
                    answered = {b["tool_use_id"] for b in history[i + 1]["content"]}
                    self.assertEqual(set(calls), answered)

    def test_tool_loop_streaming_and_request_shape(self):
        api, brain, ctx = self.make([
            reply([text("Let me check."), tool("toolu_1", "echo", {"msg": "hi"})], "tool_use"),
            reply([text("All done, sir.")]),
        ])
        streamed, statuses, activities = [], [], []
        result = brain.run_turn("say hi", ctx, on_text=streamed.append, on_status=statuses.append,
                                on_activity=activities.append)
        self.assertEqual(result.text, "All done, sir.")
        self.assertEqual(self.calls, [{"msg": "hi"}])
        self.assertIn("Let me check.", "".join(streamed))
        self.assertEqual(statuses, ["Using echo"])
        self.assertEqual(activities, ["Working…"])
        first, second = api.requests
        self.assertEqual(first["cache_control"], {"type": "ephemeral"})
        self.assertEqual(first["system"][-1]["cache_control"], {"type": "ephemeral"})
        self.assertIn({"type": "web_search_20250305", "name": "web_search", "max_uses": 5}, first["tools"])
        self.assertEqual(first["model"], "claude-sonnet-5-5")
        self.assertIn("Your tools right now: echo, web_search", first["system"][0]["text"])
        results = second["messages"][-1]["content"]
        self.assertEqual(results[0], {"type": "tool_result", "tool_use_id": "toolu_1", "content": "echo: hi"})
        assistant = second["messages"][1]["content"]
        self.assertEqual([b["type"] for b in assistant], ["text", "tool_use"])
        self.assertEqual(set(assistant[1]), {"type", "id", "name", "input"})
        self.assertEqual([m["role"] for m in brain.history], ["user", "assistant", "user", "assistant"])
        self.assertIn("input tokens", brain.usage_line())

    def test_pause_turn_and_web_search_blocks_are_sent_back(self):
        api, brain, ctx = self.make([reply(SEARCH, "pause_turn"), reply([text("Here's the news.")])])
        activities = []
        result = brain.run_turn("news?", ctx, on_activity=activities.append)
        self.assertEqual(result.text, "Here's the news.")
        self.assertEqual(activities, ["Searching the web…"])
        second = api.requests[1]
        self.assertEqual(second["messages"][-1]["role"], "assistant")
        self.assertIn("ENC-123", json.dumps(second["messages"][-1]))
        self.assertEqual(len(brain.history), 2)  # merged into one assistant message
        self.assertEqual(len(brain.history[1]["content"]), 3)

    def test_web_search_disabled_fallback(self):
        error = api_error(400, "invalid_request_error", "web search is not enabled for this organization")
        api, brain, ctx = self.make([error, reply([text("Fine without it.")])])
        self.assertEqual(brain.run_turn("hello", ctx).text, "Fine without it.")
        self.assertFalse(brain.web_search)
        self.assertFalse(any(t.get("name") == "web_search" for t in api.requests[1]["tools"]))
        self.assertNotIn("web_search", api.requests[1]["system"][0]["text"])

    def test_max_tokens_tool_call_not_executed(self):
        api, brain, ctx = self.make([
            reply([tool("toolu_9", "echo", {"msg": "partial"})], "max_tokens"),
            reply([text("I'll split it up.")]),
        ])
        result = brain.run_turn("write a huge file", ctx)
        self.assertEqual(self.calls, [])
        tool_result = api.requests[1]["messages"][-1]["content"][0]
        self.assertTrue(tool_result["is_error"])
        self.assertIn("Not executed", tool_result["content"])
        self.assertEqual(result.text, "I'll split it up.")

    def test_friendly_errors_leave_history_untouched(self):
        for response, expected in (
            (api_error(401, "authentication_error", "invalid x-api-key"), "API key"),
            (api_error(400, "invalid_request_error", "Your credit balance is too low to access the API."), "credit"),
            (api_error(404, "not_found_error", "model: nope"), "wasn't found"),
            (api_error(529, "overloaded_error", "Overloaded"), "overloaded"),
        ):
            _, brain, ctx = self.make([response])
            with self.assertRaises(BrainError) as caught:
                brain.run_turn("hi", ctx)
            self.assertIn(expected, str(caught.exception))
            self.assertEqual(brain.history, [])

    def test_missing_key_gives_setup_instructions(self):
        os.environ["ANTHROPIC_API_KEY"] = ""
        brain = Brain(self.cfg, self.reg, None)
        with self.assertRaises(BrainError) as caught:
            brain.run_turn("hi", ToolContext(cfg=self.cfg))
        self.assertEqual(str(caught.exception), NO_KEY)
        self.assertEqual(caught.exception.kind, "auth")

    def test_cancel_during_tools_leaves_valid_history(self):
        def slow(ctx, a):
            ctx.cancel.set()
            raise Cancelled()

        self.reg.add("slow", "slow tool", {}, func=slow)
        api, brain, ctx = self.make([
            reply([tool("toolu_a", "echo", {"msg": "1"}), tool("toolu_b", "slow", {}),
                   tool("toolu_c", "echo", {"msg": "3"})], "tool_use"),
            reply([text("Next request answered.")]),
        ])
        with self.assertRaises(Cancelled):
            brain.run_turn("do three things", ctx)
        self.assert_valid_history(brain.history)
        self.assertEqual(brain.history[3]["content"][-1]["text"], STOPPED_NOTE)
        ctx.cancel.clear()
        self.assertEqual(brain.run_turn("next", ctx).text, "Next request answered.")

    def test_failure_mid_turn_keeps_a_valid_record(self):
        api, brain, ctx = self.make([
            reply([tool("toolu_1", "echo", {"msg": "first"})], "tool_use"),
            api_error(500, "api_error", "Internal server error"),
            reply([text("Back again.")]),
        ])
        with self.assertRaises(BrainError):
            brain.run_turn("do it", ctx)
        self.assert_valid_history(brain.history)
        self.assertIn("This request failed", brain.history[-1]["content"][-1]["text"])
        self.assertEqual(brain.run_turn("try again", ctx).text, "Back again.")

    def test_step_limit_stops_cleanly(self):
        cfg = self.make_cfg(llm={"max_steps": 2})
        api, brain, ctx = self.make([
            reply([tool("toolu_1", "echo", {"msg": "1"})], "tool_use"),
            reply([tool("toolu_2", "echo", {"msg": "2"})], "tool_use"),
        ], cfg=cfg)
        result = brain.run_turn("loop forever", ctx)
        self.assertEqual(result.stop_reason, "step_limit")
        self.assertEqual(result.text, STEP_LIMIT_NOTE)
        self.assertEqual(len(api.requests), 2)
        self.assert_valid_history(brain.history)

    def test_context_overflow_drops_history_and_retries(self):
        api, brain, ctx = self.make([
            reply([text("First answer.")]),
            api_error(400, "invalid_request_error", "prompt is too long: 250000 tokens > 200000 maximum"),
            reply([text("Second answer.")]),
        ])
        brain.run_turn("first", ctx)
        self.assertEqual(brain.run_turn("second", ctx).text, "Second answer.")
        self.assertEqual(len(api.requests[2]["messages"]), 1)  # retried without the old history
        self.assertEqual(len(brain.history), 2)

    def test_memory_snapshot_is_stable_within_a_request(self):
        reg = build_registry(self.cfg)
        api = FakeAPI([
            reply([tool("toolu_m", "memory", {"action": "add", "text": "Prefers short answers"})], "tool_use"),
            reply([text("Noted.")]),
            reply([text("Hello again.")]),
        ])
        memory = Memory(Path(self.tmp.name) / "mem.json")
        brain = Brain(self.cfg, reg, memory, client=api.client())
        ctx = ToolContext(cfg=self.cfg, ui=self.ui, memory=memory)
        brain.run_turn("remember that I prefer short answers", ctx)
        self.assertEqual(memory.list(), ["Prefers short answers"])
        self.assertEqual(api.requests[0]["system"], api.requests[1]["system"])  # cache stays warm
        brain.run_turn("hi", ctx)
        system = api.requests[2]["system"]
        self.assertEqual(len(system), 2)
        self.assertIn("Prefers short answers", system[1]["text"])
        self.assertEqual(system[1]["cache_control"], {"type": "ephemeral"})
        self.assertNotIn("cache_control", system[0])

    def test_history_trim_and_image_strip(self):
        image = {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "AAAA"}}
        history = []
        for i in range(12):
            history.append({"role": "user", "content": [image, {"type": "text", "text": f"q{i}"}]})
            history.append({"role": "assistant", "content": [{"type": "text", "text": f"a{i}"}]})
        trimmed = trim_history(strip_images(history), 8)
        self.assertEqual(sum(1 for m in trimmed if is_turn_start(m)), 8)
        self.assertEqual(trimmed[0]["content"][1]["text"], "q4")
        self.assertNotIn('"image"', json.dumps(trimmed))

    def test_old_tool_inputs_and_outputs_are_compacted(self):
        big = "x" * 10_000
        sdk_call = anthropic.types.ToolUseBlock(id="toolu_old", name="write_file", input={"content": big, "path": "a.py"},
                                                type="tool_use")
        history = [
            {"role": "user", "content": "write a big file"},
            {"role": "assistant", "content": [sdk_call]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_old", "content": big}]},
            {"role": "assistant", "content": [{"type": "text", "text": "Done."}]},
            {"role": "user", "content": "q2"}, {"role": "assistant", "content": [{"type": "text", "text": "a2"}]},
            {"role": "user", "content": "q3"}, {"role": "assistant", "content": [{"type": "text", "text": "a3"}]},
        ]
        compacted = compact_old_turns(history)
        old_call = compacted[1]["content"][0]
        self.assertEqual(old_call["id"], "toolu_old")
        self.assertEqual(old_call["input"]["path"], "a.py")
        self.assertLess(len(old_call["input"]["content"]), 2000)
        self.assertLess(len(compacted[2]["content"][0]["content"]), 2000)
        self.assertEqual(compacted[4:], history[4:])  # recent turns untouched


class MemoryTests(Base):
    def test_add_list_remove_and_persist(self):
        path = Path(self.tmp.name) / "memory.json"
        memory = Memory(path)
        self.assertEqual(memory.as_prompt(), "")
        self.assertEqual(memory.add("  Learning   React "), "Remembered: Learning React")
        self.assertIn("Already", memory.add("learning react"))
        memory.add("Uses VS Code")
        memory.add("Likes dark mode")
        self.assertEqual(Memory(path).list(), ["Learning React", "Uses VS Code", "Likes dark mode"])
        self.assertEqual(memory.remove("2"), ["Uses VS Code"])
        self.assertEqual(memory.remove("DARK"), ["Likes dark mode"])
        self.assertEqual(memory.remove("nothing like this"), [])
        self.assertIn("- Learning React", memory.as_prompt())
        with self.assertRaises(ValueError):
            memory.add("   ")


class RegistryTests(Base):
    def test_results_errors_and_truncation(self):
        reg = ToolRegistry()

        def boom(ctx, a):
            raise ValueError("boom")

        def refuse(ctx, a):
            raise ToolError("not allowed")

        def stop(ctx, a):
            raise Cancelled()

        reg.add("boom", "fails", {}, func=boom)
        reg.add("refuse", "refuses", {}, func=refuse)
        reg.add("long", "long output", {}, func=lambda ctx, a: "y" * 50_000)
        reg.add("quiet", "returns nothing", {}, func=lambda ctx, a: None)
        reg.add("stop", "cancels", {}, func=stop)
        ctx = ToolContext(cfg=self.cfg)
        self.assertEqual(reg.execute("nope", {}, ctx).is_error, True)
        self.assertEqual(reg.execute("boom", {}, ctx).text, "ValueError: boom")
        refused = reg.execute("refuse", {}, ctx)
        self.assertTrue(refused.is_error)
        self.assertEqual(refused.text, "not allowed")
        self.assertLess(len(reg.execute("long", {}, ctx).text), 16_000)
        self.assertEqual(reg.execute("quiet", {}, ctx).text, "Done.")
        with self.assertRaises(Cancelled):
            reg.execute("stop", {}, ctx)
        self.assertEqual(reg.summarize("boom", {}), "Using boom")
        self.assertEqual(reg.activity("boom"), "Working…")
        self.assertIn("characters omitted", truncate("z" * 20_000, 1000))
        self.assertEqual(ToolResult("").to_content(), "(no output)")

    def test_phase_two_tools(self):
        reg = build_registry(self.cfg)
        self.assertEqual(reg.names(), ["memory"])
        for schema in reg.schemas():
            self.assertEqual(set(schema), {"name", "description", "input_schema"})
            self.assertEqual(schema["input_schema"]["type"], "object")
        memory = Memory(Path(self.tmp.name) / "m.json")
        ctx = ToolContext(cfg=self.cfg, memory=memory)
        self.assertIn("Remembered", reg.execute("memory", {"action": "add", "text": "Studies CS"}, ctx).text)
        self.assertIn("1. Studies CS", reg.execute("memory", {"action": "list"}, ctx).text)
        self.assertIn("Forgot", reg.execute("memory", {"action": "remove", "text": "cs"}, ctx).text)
        self.assertTrue(reg.execute("memory", {"action": "add"}, ctx).is_error)
        self.assertEqual(reg.summarize("memory", {"action": "add", "text": "x"}), "Remembering: x")


class PromptTests(Base):
    def test_phase_two_prompt_only_mentions_available_tools(self):
        prompt = build_system_prompt(self.cfg, ["memory"], web_search=True)
        self.assertIn("Jarvis", prompt)
        self.assertIn("Your tools right now: memory, web_search", prompt)
        self.assertIn("memory tool", prompt)
        for missing in ("run_shell", "write_file", "point_at", "type_text", "# Coding"):
            self.assertNotIn(missing, prompt)
        self.assertNotIn("{", prompt)

    def test_later_tools_unlock_their_guidance(self):
        names = ["memory", "run_shell", "write_file", "edit_file", "run_applescript", "type_text",
                 "get_selected_text", "take_screenshot", "point_at"]
        prompt = build_system_prompt(self.cfg, names, web_search=False)
        self.assertIn("# Coding", prompt)
        self.assertIn(str(self.cfg.paths.workspace), prompt)
        self.assertIn("point_at", prompt)
        self.assertNotIn("web_search", prompt)
        self.assertNotIn("{", prompt)

    def test_turn_context(self):
        typed = turn_context(self.cfg, source="cli")
        self.assertIn("typed in the terminal", typed)
        self.assertIn("Reply: shown as text", typed)
        spoken = turn_context(self.cfg, frontmost={"name": "Safari", "window": "Docs"}, source="voice")
        self.assertIn('Safari, window "Docs"', spoken)
        self.assertIn("speech recognition", spoken)
        self.assertIn("Reply: spoken aloud", spoken)
        tutor = turn_context(self.cfg, tutor=True, shot=SimpleNamespace(width=1280, height=800))
        self.assertIn("1280x800", tutor)


class ChatTests(Base):
    def chat(self, responses):
        api = FakeAPI(responses)
        out = io.StringIO()
        ui = ConsoleUI(stream=out, interactive=False, color=False)
        memory = Memory(Path(self.tmp.name) / "memory.json")
        brain = Brain(self.cfg, build_registry(self.cfg), memory, client=api.client())
        return api, cli.Chat(self.cfg, ui=ui, brain=brain, memory=memory), out

    def test_reply_streams_into_the_terminal(self):
        api, chat, out = self.chat([reply([text("Good evening, sir.")])])
        self.assertEqual(chat.ask("hello"), "Good evening, sir.")
        self.assertIn("jarvis › Good evening, sir.", out.getvalue())
        sent = api.requests[0]["messages"][0]["content"][0]["text"]
        self.assertTrue(sent.startswith("<context>"))
        self.assertIn("Reply: shown as text", sent)
        self.assertTrue(sent.endswith("hello"))

    def test_search_and_memory_show_progress(self):
        api, chat, out = self.chat([
            reply(SEARCH + [text("Found it."), tool("toolu_1", "memory", {"action": "add", "text": "Learning React"})],
                  "tool_use"),
            reply([text("Noted.")]),
        ])
        self.assertEqual(chat.ask("look it up and remember I'm learning React"), "Noted.")
        shown = out.getvalue()
        self.assertIn("· Searching the web…", shown)
        self.assertIn("· Remembering: Learning React", shown)
        self.assertIn("jarvis › Noted.", shown)
        self.assertEqual(chat.memory.list(), ["Learning React"])

    def test_errors_are_shown_not_raised(self):
        api, chat, out = self.chat([api_error(401, "authentication_error", "invalid x-api-key")])
        self.assertIsNone(chat.ask("hi"))
        self.assertIn("Error: My API key was rejected", out.getvalue())

    def test_commands(self):
        api, chat, out = self.chat([reply([text("Hi.")])])
        chat.ask("hi")
        chat.memory.add("Likes dark mode")
        printed = io.StringIO()
        with mock.patch("sys.stdout", printed):
            for line in ("/memory", "/forget dark", "/usage", "/help", "/new", "/bogus"):
                self.assertIsNone(cli.handle_command(chat, line))
            self.assertEqual(cli.handle_command(chat, "/quit"), "quit")
        shown = printed.getvalue()
        self.assertIn("1. Likes dark mode", shown)
        self.assertIn("Forgot: Likes dark mode", shown)
        self.assertIn("input tokens", shown)
        self.assertIn("Unknown command /bogus", shown)
        self.assertEqual(chat.brain.history, [])
        self.assertEqual(chat.memory.list(), [])

    def test_whole_terminal_session(self):
        api, chat, out = self.chat([reply([text("Hello there.")])])
        printed = io.StringIO()
        with mock.patch("sys.stdout", printed), mock.patch("builtins.input", side_effect=["hello", "", "/quit"]):
            self.assertEqual(cli.run_cli(self.cfg, chat=chat), 0)
        self.assertIn("is ready", printed.getvalue())
        self.assertIn("jarvis › Hello there.", out.getvalue())

    def test_one_shot_question(self):
        api, chat, out = self.chat([reply([text("Tokyo.")])])
        self.assertEqual(cli.run_once(self.cfg, "capital of Japan?", chat=chat), 0)
        self.assertIn("Tokyo.", out.getvalue())

    def test_no_key_explains_instead_of_starting(self):
        os.environ["ANTHROPIC_API_KEY"] = ""
        printed = io.StringIO()
        with mock.patch("sys.stdout", printed), mock.patch("builtins.input", side_effect=AssertionError("no prompt")):
            self.assertEqual(cli.run_cli(self.cfg), 1)
            self.assertEqual(cli.run_once(self.cfg, "hi"), 1)
        self.assertIn("ANTHROPIC_API_KEY=", printed.getvalue())


if __name__ == "__main__":
    unittest.main()
