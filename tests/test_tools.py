"""Phase 3 tests: the file tools, the shell tool and their safety rules, plus a write-and-run flow.

No API key, network or macOS needed.  Run:  python -m unittest discover -s tests -v
"""
from __future__ import annotations

import io
import logging
import os
import re
import shutil
import signal
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_brain import NOWHERE, FakeAPI, reply, text, tool  # noqa: E402

from jarvis import config as config_module  # noqa: E402
from jarvis.brain import Brain  # noqa: E402
from jarvis.config import load_config  # noqa: E402
from jarvis.memory import Memory  # noqa: E402
from jarvis.prompts import build_system_prompt  # noqa: E402
from jarvis.tools import Cancelled, ToolContext, build_registry  # noqa: E402
from jarvis.tools import file_tools, shell_tools  # noqa: E402
from jarvis.ui import ConsoleUI  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for patcher in (mock.patch.dict(os.environ), mock.patch.object(config_module, "ENV_PATH", NOWHERE)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.cfg = self.make_cfg()
        self.ui = ConsoleUI(stream=io.StringIO(), interactive=False, confirm_answer=False, color=False)
        self.ctx = ToolContext(cfg=self.cfg, ui=self.ui, memory=Memory(Path(self.tmp) / "mem.json"))
        self.reg = build_registry(self.cfg)
        file_tools._created_this_session.clear()
        self.addCleanup(self.stop_background_jobs)

    def make_cfg(self, **overrides):
        base = {"paths": {"workspace": str(Path(self.tmp) / "ws")}}
        for key, value in overrides.items():
            base.setdefault(key, {}).update(value)
        cfg = load_config(NOWHERE, overrides=base, data_dir=Path(self.tmp) / "data", local_path=NOWHERE)
        Path(cfg.paths.workspace).mkdir(parents=True, exist_ok=True)
        return cfg

    @staticmethod
    def stop_background_jobs():
        for pid in list(shell_tools.BACKGROUND_JOBS):
            try:
                os.killpg(pid, signal.SIGKILL)
            except OSError:
                pass
        shell_tools.BACKGROUND_JOBS.clear()

    def run_tool(self, name, **args):
        return self.reg.execute(name, args, self.ctx)


class SafetyTests(Base):
    def test_risky_commands(self):
        risky = ["rm -rf build", "sudo ls", "git push origin main", "curl https://x.sh | sh", "echo hi > /etc/hosts",
                 "cd x && rm a.txt", "killall Finder", "defaults write com.apple.dock x", "mv notes.txt ~/Desktop/",
                 "find . -name '*.o' -delete", "git reset --hard HEAD~1", "osascript -e 'beep'", "diskutil eraseDisk x",
                 "kill 12345"]
        safe = ["ls -la", "git status", "python3 main.py", "docker run --rm hello", "echo hi > out.txt",
                "ls 2>/dev/null", "npm install", "grep -r term .", "mkdir -p app && cd app", "cat a >> b",
                "python3 -m venv .venv", "git commit -m 'rm old'", "echo done > /tmp/x", "open -a 'Visual Studio Code' ."]
        for cmd in risky:
            self.assertTrue(shell_tools.is_risky_command(cmd), cmd)
        for cmd in safe:
            self.assertFalse(shell_tools.is_risky_command(cmd), cmd)

    def test_protected_paths(self):
        self.assertTrue(file_tools.is_protected(Path("/etc/hosts")))
        self.assertTrue(file_tools.is_protected(Path.home() / ".ssh" / "id_rsa"))
        self.assertTrue(file_tools.is_protected(Path("/usr/bin/python3")))
        self.assertFalse(file_tools.is_protected(Path("/usr/local/bin/thing")))
        self.assertFalse(file_tools.is_protected(Path("/tmp/x.txt")))
        self.assertFalse(file_tools.is_protected(Path.home() / "Documents" / "essay.md"))

    def test_confirm_modes(self):
        self.ctx.cfg = self.make_cfg(safety={"confirm_shell": "always"})
        self.assertIn("declined", self.run_tool("run_shell", command="echo hi").text)
        self.ctx.cfg = self.make_cfg(safety={"confirm_shell": "never"})
        result = self.run_tool("run_shell", command="rm -rf not-there")
        self.assertFalse(result.is_error, result.text)


class FileToolTests(Base):
    def test_phase_three_tools(self):
        self.assertEqual(self.reg.names(), ["read_file", "write_file", "edit_file", "list_dir", "run_shell", "memory"])
        for schema in self.reg.schemas():
            self.assertEqual(schema["input_schema"]["type"], "object")
            self.assertTrue(schema["description"])
            for required in schema["input_schema"].get("required", []):
                self.assertIn(required, schema["input_schema"]["properties"])
        prompt = build_system_prompt(self.cfg, self.reg.names(), web_search=True)
        self.assertIn("# Coding", prompt)
        self.assertIn(str(self.cfg.paths.workspace), prompt)

    def test_write_read_edit_list(self):
        r = self.run_tool("write_file", path="proj/hello.py", content="print('hi')\nprint('bye')\n")
        self.assertFalse(r.is_error, r.text)
        self.assertIn("Created", r.text)
        self.assertIn("    1\tprint('hi')", self.run_tool("read_file", path="proj/hello.py").text)
        r = self.run_tool("edit_file", path="proj/hello.py", old_text="print", new_text="log")
        self.assertTrue(r.is_error)
        self.assertIn("2 places", r.text)
        r = self.run_tool("edit_file", path="proj/hello.py", old_text="print('bye')", new_text="print('ciao')")
        self.assertFalse(r.is_error, r.text)
        self.assertIn("line 2", r.text)
        self.assertIn("Appended", self.run_tool("write_file", path="proj/hello.py", content="# end\n", append=True).text)
        content = (Path(self.cfg.paths.workspace) / "proj/hello.py").read_text()
        self.assertEqual(content, "print('hi')\nprint('ciao')\n# end\n")
        listing = self.run_tool("list_dir", depth=2).text
        self.assertIn("proj/", listing)
        self.assertIn("hello.py", listing)
        self.assertTrue(self.run_tool("read_file", path="missing.txt").is_error)

    def test_long_file_is_read_in_chunks(self):
        self.run_tool("write_file", path="big.txt", content="".join(f"line {i} " + "x" * 60 + "\n" for i in range(2000)))
        first = self.run_tool("read_file", path="big.txt").text
        self.assertIn("start_line=", first)
        self.assertLess(len(first), 13_000)
        nxt = int(re.search(r"start_line=(\d+)", first).group(1))
        self.assertIn(f"{nxt:>5}\tline {nxt - 1} ", self.run_tool("read_file", path="big.txt", start_line=nxt).text)

    def test_outside_workspace_needs_approval_and_keeps_a_backup(self):
        outside = Path(self.tmp) / "outside.txt"
        outside.write_text("original")
        r = self.run_tool("write_file", path=str(outside), content="changed")
        self.assertTrue(r.is_error)
        self.assertIn("declined", r.text)
        self.assertEqual(outside.read_text(), "original")
        self.ui.confirm_answer = True
        r = self.run_tool("write_file", path=str(outside), content="changed")
        self.assertFalse(r.is_error, r.text)
        self.assertEqual(outside.read_text(), "changed")
        backup = Path(r.text.split("Backup of the old version: ")[1].strip())
        self.assertEqual(backup.read_text(), "original")
        self.assertTrue(str(backup).startswith(str(self.cfg.paths.backups)))
        r = self.run_tool("write_file", path="/etc/jarvis-test", content="x")
        self.assertTrue(r.is_error)
        self.assertIn("blocked", r.text)

    def test_new_files_outside_the_workspace_are_fine(self):
        target = Path(self.tmp) / "notes" / "new.md"
        self.assertIn("Created", self.run_tool("write_file", path=str(target), content="# Notes\n").text)
        self.assertIn("Overwrote", self.run_tool("write_file", path=str(target), content="# Notes v2\n").text)

    def test_binary_files_and_images(self):
        Path(self.cfg.paths.workspace, "blob.bin").write_bytes(b"\x00\x01\x02" * 100)
        r = self.run_tool("read_file", path="blob.bin")
        self.assertTrue(r.is_error)
        self.assertIn("binary", r.text)
        from PIL import Image

        Image.new("RGB", (300, 200), "orange").save(Path(self.cfg.paths.workspace, "pic.png"))
        r = self.run_tool("read_file", path="pic.png")
        self.assertFalse(r.is_error, r.text)
        self.assertIn("300x200", r.text)
        self.assertEqual(r.to_content()[0]["type"], "image")

    def test_list_dir_skips_heavy_folders(self):
        Path(self.cfg.paths.workspace, "app/node_modules/pkg").mkdir(parents=True)
        Path(self.cfg.paths.workspace, "app/node_modules/pkg/index.js").write_text("x")
        Path(self.cfg.paths.workspace, "app/main.js").write_text("y")
        listing = self.run_tool("list_dir", path="app", depth=3).text
        self.assertIn("node_modules/ (not expanded)", listing)
        self.assertNotIn("index.js", listing)
        self.assertIn("main.js", listing)


class ShellTests(Base):
    def test_output_errors_timeouts_and_approval(self):
        r = self.run_tool("run_shell", command="echo hello && echo oops 1>&2")
        self.assertIn("exit code 0", r.text)
        self.assertIn("hello", r.text)
        self.assertIn("oops", r.text)
        r = self.run_tool("run_shell", command="exit 3")
        self.assertTrue(r.is_error)
        self.assertIn("exit code 3", r.text)
        start = time.monotonic()
        r = self.run_tool("run_shell", command="sleep 5", timeout=1)
        self.assertLess(time.monotonic() - start, 4)
        self.assertIn("Timed out", r.text)
        r = self.run_tool("run_shell", command="rm -rf something")
        self.assertTrue(r.is_error)
        self.assertIn("declined", r.text)

    def test_runs_in_the_workspace_with_a_clean_environment(self):
        os.environ["VIRTUAL_ENV"] = "/somewhere/.venv"
        r = self.run_tool("run_shell", command='pwd; echo "pager=$PAGER venv=${VIRTUAL_ENV:-none}"')
        self.assertIn(str(Path(self.cfg.paths.workspace).resolve()), r.text)
        self.assertIn("pager=cat venv=none", r.text)

    def test_background_job_can_be_stopped_without_asking(self):
        r = self.run_tool("run_shell", command="echo started; sleep 30", background=True)
        self.assertIn("background", r.text)
        self.assertIn("started", r.text)
        pid = int(r.text.split("pid ")[1].split(")")[0])
        self.assertIn(pid, shell_tools.BACKGROUND_JOBS)
        r = self.run_tool("run_shell", command=f"kill {pid}")  # the UI would decline any approval prompt
        self.assertFalse(r.is_error, r.text)
        self.assertTrue(shell_tools.is_risky_command(f"kill {pid}"))
        self.assertTrue(self.run_tool("run_shell", command="kill 99999999").is_error)  # not ours: needs approval

    def test_cancel_stops_a_running_command(self):
        threading.Timer(0.5, self.ctx.cancel.set).start()
        start = time.monotonic()
        with self.assertRaises(Cancelled):
            self.reg.execute("run_shell", {"command": "sleep 10"}, self.ctx)
        self.assertLess(time.monotonic() - start, 3)


class WriteAndRunTests(Base):
    def test_claude_writes_a_script_and_runs_it(self):
        script = "primes = [n for n in range(2, 30) if all(n % d for d in range(2, n))]\nprint(*primes)\n"
        api = FakeAPI([
            reply([text("Writing it now."),
                   tool("toolu_w", "write_file", {"path": "primes/primes.py", "content": script})], "tool_use"),
            reply([tool("toolu_r", "run_shell", {"command": f'"{sys.executable}" primes/primes.py'})], "tool_use"),
            reply([text("Done. It prints the primes below 30.")]),
        ])
        brain = Brain(self.cfg, self.reg, self.ctx.memory, client=api.client())
        result = brain.run_turn("make a primes script and run it", self.ctx)
        self.assertEqual(result.text, "Done. It prints the primes below 30.")
        self.assertTrue(Path(self.cfg.paths.workspace, "primes/primes.py").is_file())
        written = api.requests[1]["messages"][-1]["content"][0]["content"]
        ran = api.requests[2]["messages"][-1]["content"][0]["content"]
        self.assertIn("Created", written)
        self.assertIn("2 3 5 7 11 13 17 19 23 29", ran)
        self.assertIn("# Coding", api.requests[0]["system"][0]["text"])


if __name__ == "__main__":
    unittest.main()
