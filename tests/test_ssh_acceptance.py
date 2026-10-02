"""Offline acceptance argument/fixture guards; never connects to SSH."""

import argparse
import ast
import base64
import hashlib
import importlib.util
import inspect
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ssh_acceptance.py"
SPEC = importlib.util.spec_from_file_location("ssh_acceptance", SCRIPT)
acceptance = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(acceptance)


def sha(data):
    return hashlib.sha256(data).hexdigest()


class ConfigGuards(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.parent = Path(self.temp.name).resolve()
        self.local = self.parent / "wtb-acceptance-test"
        self.local.mkdir()
        self.path = self.parent / "config.json"
        self.report = self.parent / "report.json"
        self.config = {
            "local_root": str(self.local),
            "remote": {"kind": "ssh", "host": "fixture-test", "root": "/tmp/wtb-acceptance-test"},
            "state_dir": str(self.parent / "wtb-acceptance-state"),
            "acceptance": {"fixture_id": "wtb-acceptance-test", "expected_head": "1" * 40,
                           "expected_branch": "wtb-acceptance", "marker_sha256": "2" * 64,
                           "initial_hashes": {"AGENTS.md": "3" * 64, "CONTEXT.md": "4" * 64}}}

    def validate(self):
        self.path.write_text(json.dumps(self.config), encoding="utf-8")
        with mock.patch.object(acceptance.subprocess, "run", side_effect=self.ssh_config_only):
            return acceptance.validate_config(self.path, self.report)

    @staticmethod
    def ssh_config_only(command, **kwargs):
        if "-G" not in command:
            raise AssertionError("offline test must not connect to SSH")
        return subprocess.CompletedProcess(command, 0,
            b"hostname fixture.example.invalid\nuser fixture-test\nport 22\n", b"")

    def test_valid_configuration_is_read_only_and_does_not_connect(self):
        self.validate()
        self.assertFalse(self.report.exists())
        self.assertFalse(Path(self.config["state_dir"]).exists())

    def test_arbitrary_project_root_is_rejected(self):
        self.config["local_root"] = str(self.parent / "ordinary-project")
        with self.assertRaisesRegex(ValueError, "basename"):
            self.validate()

    def test_local_endpoint_and_unsafe_remote_path_are_rejected(self):
        self.config["remote"]["kind"] = "local"
        self.config["remote"]["root"] = str(self.parent / "wtb-acceptance-other")
        with self.assertRaisesRegex(ValueError, "remote.kind=ssh"):
            self.validate()
        self.config["remote"] = {"kind": "ssh", "host": "fixture-test",
                                 "root": "/tmp/real/../wtb-acceptance-test"}
        with self.assertRaisesRegex(ValueError, "canonical"):
            self.validate()

    def test_invalid_contract_hash_or_branch_is_rejected(self):
        self.config["acceptance"]["marker_sha256"] = "not-a-hash"
        with self.assertRaisesRegex(ValueError, "SHA256"):
            self.validate()
        self.config["acceptance"]["marker_sha256"] = "2" * 64
        self.config["acceptance"]["expected_branch"] = "main"
        with self.assertRaisesRegex(ValueError, "branch"):
            self.validate()

    def test_existing_state_or_report_is_not_reused(self):
        state = Path(self.config["state_dir"])
        state.mkdir()
        (state / "baseline.json").write_text("private-existing-data", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "fresh"):
            self.validate()
        self.assertEqual((state / "baseline.json").read_text(encoding="utf-8"), "private-existing-data")
        self.config["state_dir"] = str(self.parent / "wtb-acceptance-new-state")
        self.report.write_text("private-report", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.validate()
        self.assertEqual(self.report.read_text(encoding="utf-8"), "private-report")

    def test_private_outputs_cannot_enter_fixture_or_source(self):
        self.report = self.local / "report.json"
        with self.assertRaisesRegex(ValueError, "outside"):
            self.validate()
        self.report = acceptance.SOURCE_ROOT / "acceptance-report-test.json"
        with self.assertRaisesRegex(ValueError, "outside"):
            self.validate()

    def test_unsafe_ssh_host_or_port_is_rejected_before_connection(self):
        self.config["remote"]["host"] = "-oProxyCommand=anything"
        with self.assertRaisesRegex(ValueError, "host"):
            self.validate()
        self.config["remote"]["host"] = "fixture-test"
        self.config["remote"]["port"] = True
        with self.assertRaisesRegex(ValueError, "port"):
            self.validate()

    def test_required_cli_arguments(self):
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit) as raised:
            acceptance.main([])
        self.assertEqual(raised.exception.code, 2)

    def test_ssh_alias_keeps_configured_port_unless_explicitly_overridden(self):
        self.validate()
        with mock.patch.object(acceptance.subprocess, "run", side_effect=self.ssh_config_only):
            harness = acceptance.Acceptance(self.path, self.report)
        response = subprocess.CompletedProcess([], 0, b'{"ok":true,"result":{}}', b"")
        with mock.patch.object(acceptance.subprocess, "run", return_value=response) as run:
            harness.fixture("remote")
            self.assertNotIn("-p", run.call_args.args[0])
        self.config["remote"]["port"] = 2345
        self.validate()
        with mock.patch.object(acceptance.subprocess, "run", side_effect=self.ssh_config_only):
            harness = acceptance.Acceptance(self.path, self.report)
        with mock.patch.object(acceptance.subprocess, "run", return_value=response) as run:
            harness.fixture("remote")
            command = run.call_args.args[0]
            self.assertEqual(command[command.index("-p") + 1], "2345")


class FixtureGuards(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / "wtb-acceptance-test"
        self.root.mkdir()
        marker = json.dumps({"schema": 1, "purpose": "wtb-acceptance",
                             "fixture_id": "wtb-acceptance-test"}).encode("utf-8")
        documents = {"AGENTS.md": b"test agents\n", "CONTEXT.md": b"test context\n"}
        (self.root / acceptance.MARKER).write_bytes(marker)
        for name, data in documents.items():
            (self.root / name).write_bytes(data)
        self.git("init", "-q", "-b", "wtb-acceptance")
        self.git("config", "core.autocrlf", "false")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "user.name", "Acceptance Fixture")
        self.git("add", ".")
        self.git("commit", "-qm", "fixture")
        self.request = {"root": str(self.root), "op": "inspect", "initial": True,
                        "acceptance": {"fixture_id": "wtb-acceptance-test",
                        "expected_head": self.git("rev-parse", "HEAD"),
                        "expected_branch": "wtb-acceptance", "marker_sha256": sha(marker),
                        "initial_hashes": {name: sha(data) for name, data in documents.items()}}}

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), *args], check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.decode().strip()

    def write_request(self, path="AGENTS.md"):
        return {**self.request, "initial": False, "op": "write", "path": path,
                "expected": sha(b"test agents\n"), "data": base64.b64encode(b"changed\n").decode()}

    def test_initial_guard_checks_marker_head_and_content(self):
        result = acceptance.fixture_operation(self.request)
        self.assertEqual(result["hashes"]["AGENTS.md"], sha(b"test agents\n"))
        self.request["acceptance"]["expected_head"] = "0" * 40
        with self.assertRaisesRegex(ValueError, "HEAD"):
            acceptance.fixture_operation(self.request)
        self.assertEqual((self.root / "AGENTS.md").read_bytes(), b"test agents\n")
        self.request["acceptance"]["expected_head"] = result["head"]
        self.request["acceptance"]["initial_hashes"]["AGENTS.md"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "initial fixture content hash"):
            acceptance.fixture_operation(self.request)

    def test_marker_mismatch_stops_write(self):
        (self.root / acceptance.MARKER).write_bytes(b"{}")
        with self.assertRaisesRegex(ValueError, "marker hash"):
            acceptance.fixture_operation(self.write_request())
        self.assertEqual((self.root / "AGENTS.md").read_bytes(), b"test agents\n")

    def test_injection_rejects_other_paths_and_stale_hashes(self):
        for path in ("../outside.txt", acceptance.MARKER, "README.md"):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, "only edit"):
                acceptance.fixture_operation(self.write_request(path))
        request = self.write_request()
        request["expected"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "changed before"):
            acceptance.fixture_operation(request)
        self.assertEqual((self.root / "AGENTS.md").read_bytes(), b"test agents\n")

    def test_dirty_initial_fixture_is_rejected(self):
        (self.root / "CONTEXT.md").write_bytes(b"dirty\n")
        with self.assertRaisesRegex(ValueError, "clean Git status"):
            acceptance.fixture_operation(self.request)

    def test_git_environment_cannot_redirect_fixture_validation(self):
        with mock.patch.dict(os.environ, {"GIT_DIR": str(self.root / "nonexistent.git"),
                                          "GIT_WORK_TREE": str(self.root.parent)}):
            result = acceptance.fixture_operation(self.request)
        self.assertEqual(result["root"], str(self.root))

    def test_remote_program_is_python39_compatible_and_guarded(self):
        program = inspect.getsource(acceptance.fixture_operation)
        ast.parse(program, feature_version=(3, 9))
        request = self.write_request()
        request["acceptance"] = {**request["acceptance"], "marker_sha256": "0" * 64}
        namespace = {}
        exec(program, namespace)
        with self.assertRaisesRegex(ValueError, "marker hash"):
            namespace["fixture_operation"](request)
        self.assertEqual((self.root / "AGENTS.md").read_bytes(), b"test agents\n")


if __name__ == "__main__":
    unittest.main()
