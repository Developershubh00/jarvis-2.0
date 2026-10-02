"""Phase 1 tests: settings, API error messages and the doctor.

Run from the project folder:  python -m unittest discover -s tests -v
No network or API key needed: the Claude API is simulated.
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import anthropic  # noqa: E402
import httpx2  # noqa: E402  (the HTTP library the Anthropic SDK uses)
import yaml  # noqa: E402

from jarvis import PHASE, __version__  # noqa: E402
from jarvis import config as config_module  # noqa: E402
from jarvis.__main__ import main  # noqa: E402
from jarvis.claude_client import MissingKeyError, explain_api_error, make_client, mask_key  # noqa: E402
from jarvis.config import DEFAULTS, deep_merge, load_config  # noqa: E402
from jarvis.doctor import FAIL, Report, check_api, run_doctor  # noqa: E402

NOWHERE = "/nonexistent/jarvis-test.yaml"


def fake_claude(handler) -> anthropic.Anthropic:
    """An Anthropic client whose HTTP requests are answered by ``handler`` instead of the internet."""
    transport = httpx2.MockTransport(handler)
    return anthropic.Anthropic(api_key="sk-ant-test", http_client=httpx2.Client(transport=transport), max_retries=0)


def api_error(status: int, kind: str, message: str):
    def handler(request):
        return httpx2.Response(status, json={"type": "error", "error": {"type": kind, "message": message}})
    return handler


def claude_says(text: str):
    def handler(request):
        body = json.loads(request.content)
        return httpx2.Response(200, json={
            "id": "msg_test", "type": "message", "role": "assistant", "model": body["model"],
            "content": [{"type": "text", "text": text}], "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 12, "output_tokens": 4},
        })
    return handler


class Hermetic(unittest.TestCase):
    """Isolates tests from your real .env, environment variables and data folder."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for patcher in (mock.patch.dict(os.environ),
                        mock.patch.object(config_module, "ENV_PATH", Path(NOWHERE))):
            patcher.start()
            self.addCleanup(patcher.stop)
        os.environ.pop("JARVIS_MODEL", None)

    def write(self, name: str, text: str) -> Path:
        path = Path(self.tmp.name) / name
        path.write_text(text, encoding="utf-8")
        return path

    def load(self, main_text=None, local_text=None, **kwargs):
        main_file = self.write("config.yaml", main_text) if main_text is not None else NOWHERE
        local_file = self.write("config.local.yaml", local_text) if local_text is not None else NOWHERE
        kwargs.setdefault("overrides", {})
        kwargs["overrides"] = deep_merge({"paths": {"workspace": str(Path(self.tmp.name) / "ws")}}, kwargs["overrides"])
        return load_config(main_file, local_path=local_file, data_dir=self.tmp.name, **kwargs)


class ConfigTests(Hermetic):
    def test_defaults_when_no_files(self):
        cfg = self.load()
        self.assertEqual(cfg.llm.model, DEFAULTS["llm"]["model"])
        self.assertEqual(cfg.hotkeys.talk, "ctrl+alt+c")
        self.assertEqual(cfg.warnings, [])
        self.assertEqual(cfg.loaded_files, [])

    def test_shipped_config_yaml_matches_defaults(self):
        with open(ROOT / "config.yaml", encoding="utf-8") as f:
            self.assertEqual(yaml.safe_load(f), DEFAULTS)

    def test_local_file_overrides_main_file(self):
        cfg = self.load("llm:\n  model: from-main\n  max_steps: 12\n", "llm:\n  model: from-local\n")
        self.assertEqual(cfg.llm.model, "from-local")
        self.assertEqual(cfg.llm.max_steps, 12)
        self.assertEqual(cfg.llm.max_tokens, DEFAULTS["llm"]["max_tokens"])
        self.assertEqual([p.name for p in cfg.loaded_files], ["config.yaml", "config.local.yaml"])

    def test_command_line_beats_environment_beats_files(self):
        with mock.patch.dict(os.environ, {"JARVIS_MODEL": "from-env"}):
            self.assertEqual(self.load("llm:\n  model: from-file\n").llm.model, "from-env")
            cfg = self.load("llm:\n  model: from-file\n", overrides={"llm": {"model": "from-cli"}})
            self.assertEqual(cfg.llm.model, "from-cli")

    def test_typos_are_reported(self):
        warnings = " ".join(self.load("hotkey:\n  talk: f5\nllm:\n  modle: x\n").warnings)
        self.assertIn("'hotkey'", warnings)
        self.assertIn("'llm.modle'", warnings)

    def test_bad_values_fall_back_to_defaults(self):
        cfg = self.load("safety:\n  confirm_shell: sometimes\nllm:\n  max_steps: lots\n")
        self.assertEqual(cfg.safety.confirm_shell, "risky")
        self.assertEqual(cfg.llm.max_steps, DEFAULTS["llm"]["max_steps"])
        self.assertEqual(len(cfg.warnings), 2)

    def test_half_edited_sections_are_ignored(self):
        cfg = self.load(local_text="llm:\nhotkeys: f5\n")
        self.assertEqual(cfg.llm.model, DEFAULTS["llm"]["model"])
        self.assertEqual(cfg.hotkeys.talk, "ctrl+alt+c")
        self.assertTrue(any("'hotkeys'" in w for w in cfg.warnings))

    def test_broken_yaml_does_not_crash(self):
        cfg = self.load("llm: [unclosed\n")
        self.assertTrue(any("Couldn't read" in w for w in cfg.warnings))
        self.assertEqual(cfg.llm.model, DEFAULTS["llm"]["model"])

    def test_paths(self):
        cfg = self.load()
        self.assertEqual(cfg.paths.workspace, (Path(self.tmp.name) / "ws").resolve())
        self.assertEqual(cfg.paths.logs, Path(self.tmp.name) / "logs")
        self.assertEqual(cfg.paths.memory_file, Path(self.tmp.name) / "memory.json")

    def test_defaults_are_never_modified(self):
        before = json.dumps(DEFAULTS, sort_keys=True)
        self.load("llm:\n  max_steps: 3\nsafety:\n  confirm_shell: ALWAYS\n")
        deep_merge(DEFAULTS, {"llm": {"model": "x"}})
        self.assertEqual(json.dumps(DEFAULTS, sort_keys=True), before)


class ApiErrorTests(Hermetic):
    def explain(self, handler, model="claude-test"):
        with self.assertRaises(Exception) as caught:
            fake_claude(handler).messages.create(
                model=model, max_tokens=5, messages=[{"role": "user", "content": "hi"}])
        return explain_api_error(caught.exception, model)

    def test_bad_key(self):
        self.assertIn("rejected", self.explain(api_error(401, "authentication_error", "invalid x-api-key")))

    def test_unknown_model(self):
        self.assertIn("'claude-test'", self.explain(api_error(404, "not_found_error", "model: claude-test")))

    def test_no_credits(self):
        message = "Your credit balance is too low to access the Anthropic API. Please go to Plans & Billing."
        self.assertIn("credit balance", self.explain(api_error(400, "invalid_request_error", message)))

    def test_rate_limited(self):
        self.assertIn("Rate limited", self.explain(api_error(429, "rate_limit_error", "slow down")))

    def test_overloaded(self):
        self.assertIn("overloaded", self.explain(api_error(529, "overloaded_error", "Overloaded")))

    def test_offline(self):
        def handler(request):
            raise httpx2.ConnectError("no route to host", request=request)
        self.assertIn("internet", self.explain(handler))

    def test_missing_key(self):
        os.environ["ANTHROPIC_API_KEY"] = ""
        with self.assertRaises(MissingKeyError) as caught:
            make_client()
        self.assertIn(".env", explain_api_error(caught.exception))

    def test_keys_are_masked(self):
        key = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz"
        self.assertEqual(mask_key(key), "sk-ant-api…wxyz")
        self.assertNotIn("mnopq", mask_key(key))


class DoctorTests(Hermetic):
    def report(self):
        out = io.StringIO()
        return Report(out, color=False), out

    def test_api_check_passes(self):
        r, out = self.report()
        check_api(r, self.load(), client=fake_claude(claude_says("Jarvis online.")))
        self.assertEqual(r.count(FAIL), 0)
        self.assertIn("Claude replied: Jarvis online.", out.getvalue())

    def test_api_check_explains_a_bad_key(self):
        r, out = self.report()
        check_api(r, self.load(), client=fake_claude(api_error(401, "authentication_error", "invalid x-api-key")))
        self.assertEqual(r.count(FAIL), 1)
        self.assertIn("rejected", out.getvalue())

    def test_missing_key_is_a_failure(self):
        os.environ["ANTHROPIC_API_KEY"] = ""
        r, out = self.report()
        check_api(r, self.load())
        self.assertEqual(r.count(FAIL), 1)
        self.assertIn("ANTHROPIC_API_KEY", out.getvalue())

    def test_full_report(self):
        out = io.StringIO()
        code = run_doctor(self.load(), out=out, client=fake_claude(claude_says("Jarvis online.")), hardware=False)
        text = out.getvalue()
        self.assertIn(f"phase {PHASE} of 10", text)
        self.assertIn("Claude replied: Jarvis online.", text)
        self.assertIn(code, (0, 1))  # package checks depend on what's installed on this machine


class CommandLineTests(unittest.TestCase):
    def test_version(self):
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            self.assertEqual(main(["--version"]), 0)
        self.assertIn(__version__, out.getvalue())
        self.assertIn(f"phase {PHASE} of 10", out.getvalue())


if __name__ == "__main__":
    unittest.main()
