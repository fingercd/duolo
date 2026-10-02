import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))
from worktree_bridge import __main__ as cli


class CLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        subprocess.run(["git", "init", "--template=", "-b", "main", str(self.project)],
                       check=True, capture_output=True)
        self.env = dict(os.environ, PYTHONPATH=str(SRC), PYTHONUTF8="1",
                        LOCALAPPDATA=str(self.root / "registration-home"),
                        XDG_STATE_HOME=str(self.root / "registration-home"))

    def call(self, *args, cwd=None):
        process = subprocess.run([sys.executable, "-m", "worktree_bridge", *args],
                                 cwd=cwd or self.project, env=self.env, capture_output=True,
                                 text=True, encoding="utf-8", timeout=30)
        return process.returncode, process.stdout, process.stderr

    def test_init_registers_existing_repository_without_remote_connection(self):
        code, output, error = self.call("init", "--remote", "not-connected.example", "--path", "/work/repo")
        self.assertEqual(code, 0, output + error)
        registered = json.loads(output)
        self.assertTrue(registered["registered"])
        self.assertTrue(Path(registered["config_path"]).is_file())
        self.assertFalse(Path(registered["state_dir"]).exists())
        code, output, _ = self.call("init")
        self.assertEqual(code, 0, output)
        self.assertFalse(json.loads(output)["registered"])
        code, output, _ = self.call("projects", cwd=self.root)
        self.assertEqual(code, 0, output)
        self.assertEqual(len(json.loads(output)["projects"]), 1)

    def test_status_discovers_registration_and_reports_missing_service(self):
        code, _, _ = self.call("init", "--remote", "not-connected.example", "--path", "/work/repo")
        self.assertEqual(code, 0)
        nested = self.project / "src"
        nested.mkdir()
        code, output, _ = self.call("status", cwd=nested)
        self.assertEqual(code, 2)
        self.assertIn("start", json.loads(output)["error"])

    def test_mcp_discovery_errors_never_pollute_protocol_stdout(self):
        code, output, error = self.call("mcp")
        self.assertEqual(code, 2)
        self.assertEqual(output, "")
        self.assertIn("not registered", error)

    def test_watch_displays_changes_and_sanitizes_control_characters(self):
        initial = {"state": "synced", "revision": "1", "name": "Demo", "events": []}
        conflict = {"state": "conflict", "revision": "2", "name": "Demo",
                    "files": {"pending_count": 0, "conflict_count": 1},
                    "conflicts": [{"path": "AGENTS.md", "reason": "both_changed"}],
                    "events": [{"time": 1, "level": "error", "message": "conflict\x1b[31m"}]}
        output = io.StringIO()
        with mock.patch("worktree_bridge.client.get_status", side_effect=[initial, conflict, KeyboardInterrupt]), \
             mock.patch("time.sleep"), contextlib.redirect_stdout(output):
            with self.assertRaises(KeyboardInterrupt):
                cli.watch_status("unused", .1)
        self.assertIn("[SYNCED]", output.getvalue())
        self.assertIn("[CONFLICT]", output.getvalue())
        self.assertIn("AGENTS.md", output.getvalue())
        self.assertNotIn("\x1b", output.getvalue())


if __name__ == "__main__":
    unittest.main()
