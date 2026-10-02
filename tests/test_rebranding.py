"""Exercise Duolo entry points against an unchanged v0.3 project and daemon."""

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import sysconfig
import tempfile
import threading
import time
import unittest
from unittest import mock


SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))
from duolo import client, registry


class RebrandingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.project = self.root / "project"
        self.project.mkdir()
        self.environment = dict(os.environ, PYTHONPATH=str(SRC), PYTHONUTF8="1",
                                LOCALAPPDATA=str(self.root / "legacy-home"),
                                XDG_STATE_HOME=str(self.root / "legacy-home"))
        environment = mock.patch.dict(os.environ, self.environment)
        environment.start()
        self.addCleanup(environment.stop)
        initialized = self.run_command(["git", "-C", str(self.project), "init", "-q", "--template=", "-b", "main"])
        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        self.config = self.project / ".git" / "worktree-bridge" / "config.json"
        self.config.parent.mkdir()
        self.state = self.root / "legacy-state"
        self.state.mkdir()
        configuration = {
            "local_root": str(self.project), "remote": {"kind": "ssh", "host": "fixture-alias", "root": "/srv/fixture"},
            "state_dir": str(self.state), "name": "Legacy pair", "service": {"auto_sync": False},
        }
        self.config.write_text(json.dumps(configuration, indent=3) + "\n", encoding="utf-8")
        canonical = json.dumps(configuration, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        self.fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        # This is the v0.3 registry identity and storage layout. No init/migration
        # has run through the new package before these existing records are read.
        identity = json.dumps([os.path.normcase(str(self.project)), os.path.normcase(str(self.project / ".git"))],
                              separators=(",", ":"))
        self.project_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
        home = self.root / "legacy-home" / ("WorktreeBridge" if os.name == "nt" else "worktree-bridge")
        home.mkdir(parents=True)
        self.index = home / "registry.json"
        self.index.write_text(json.dumps({"schema": 1, "projects": {self.project_id: {
            "project_id": self.project_id, "local_root": str(self.project),
            "config_path": str(self.config), "name": "Legacy pair", "registered_at": 123.0,
        }}}, indent=3), encoding="utf-8")
        self.requests = []
        owner = self

        class LegacyHandler(BaseHTTPRequestHandler):
            server_version = "WorktreeBridge/0.3"

            def log_message(self, *_):
                pass

            def do_GET(self):
                owner.requests.append(("GET", self.path, dict(self.headers)))
                if self.path != "/api/status" or self.headers.get("X-WTB-Instance") != "legacy-instance":
                    self.reply({"error": "v0.3 instance guard rejected the request"}, 403)
                    return
                self.reply({"schema": 1, "version": "0.3.0", "name": "Legacy pair", "state": "synced",
                            "instance_id": "legacy-instance", "config_fingerprint": owner.fingerprint,
                            "revision": "legacy-revision", "updated_at": 123.0})

            def do_POST(self):
                owner.requests.append(("POST", self.path, dict(self.headers)))
                if (self.headers.get("X-WTB-Local") != "1"
                        or self.headers.get("X-WTB-Instance") != "legacy-instance"
                        or self.headers.get("Origin") != owner.url):
                    self.reply({"error": "v0.3 action guard rejected the request"}, 403)
                    return
                self.rfile.read(int(self.headers["Content-Length"]))
                self.reply({"ok": True, "queued": True, "id": "legacy-action"})

            def reply(self, payload, code=200):
                data = json.dumps(payload).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), LegacyHandler)
        self.url = "http://127.0.0.1:" + str(self.server.server_port)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.record = self.state / "service.json"
        self.record.write_text(json.dumps({"url": self.url, "instance_id": "legacy-instance", "pid": 123,
                                          "config_fingerprint": self.fingerprint}), encoding="utf-8")
        self.saved = {path: path.read_bytes() for path in (self.config, self.index, self.record)}

    def run_command(self, command, *, installed=False):
        environment = dict(self.environment)
        if installed:
            environment.pop("PYTHONPATH", None)
        return subprocess.run(command, cwd=self.project, env=environment, capture_output=True,
                              text=True, encoding="utf-8", timeout=30,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def assert_legacy_records_unchanged(self):
        for path, original in self.saved.items():
            self.assertEqual(path.read_bytes(), original, str(path))

    def test_existing_registration_is_idempotent_without_migration_or_new_namespace(self):
        self.assertEqual(registry.find_config(self.project), self.config)
        repeated = registry.init_project(self.project)
        self.assertFalse(repeated["registered"])
        self.assertEqual(repeated["project_id"], self.project_id)
        self.assertEqual(repeated["state_dir"], str(self.state))
        self.assertEqual(len(registry.list_projects()), 1)
        self.assertFalse((self.project / ".git" / "duolo").exists())
        self.assert_legacy_records_unchanged()

    def test_new_client_reads_and_controls_existing_v03_daemon(self):
        status = client.get_status(registry.find_config(self.project))
        self.assertEqual(status["state"], "synced")
        self.assertEqual(status["version"], "0.3.0")
        self.assertEqual(status["revision"], "legacy-revision")
        receipt = client.action(self.config, "pause")
        self.assertTrue(receipt["queued"])
        self.assertEqual(self.requests[-1][:2], ("POST", "/api/actions/pause"))
        self.assert_legacy_records_unchanged()

    def test_canonical_and_legacy_modules_operate_on_the_same_old_daemon(self):
        for module in ("duolo", "worktree_bridge"):
            with self.subTest(module=module):
                status = self.run_command([sys.executable, "-m", module, "status"])
                self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
                self.assertEqual(json.loads(status.stdout)["revision"], "legacy-revision")
                repeated = self.run_command([sys.executable, "-m", module, "init"])
                self.assertEqual(repeated.returncode, 0, repeated.stdout + repeated.stderr)
                self.assertFalse(json.loads(repeated.stdout)["registered"])
                version = self.run_command([sys.executable, "-m", module, "--version"])
                self.assertEqual(version.returncode, 0, version.stderr)
                self.assertEqual(version.stdout.strip(), "duo 0.4.0")
        self.assert_legacy_records_unchanged()

    def test_cli_help_and_registration_error_use_the_new_command(self):
        help_result = self.run_command([sys.executable, "-m", "duolo", "--help"])
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn("usage: duo", help_result.stdout)
        self.assertIn("Duolo", help_result.stdout)
        self.config.unlink()
        unregistered = self.run_command([sys.executable, "-m", "duolo", "status"])
        self.assertEqual(unregistered.returncode, 2)
        self.assertIn("duo init", json.loads(unregistered.stdout)["error"])

    def test_installed_duo_and_legacy_console_scripts_share_the_same_implementation(self):
        installed = next((distribution for distribution in metadata.distributions(name="duolo")
                          if distribution.read_text("INSTALLER")), None)
        if installed is None:
            self.skipTest("Install Duolo in an isolated test environment to exercise real console scripts")
        if installed.version != "0.4.0":
            self.skipTest("Installed Duolo version does not match this checkout")
        scripts = Path(sysconfig.get_path("scripts"))
        suffix = ".exe" if os.name == "nt" else ""
        for name in ("duo", "wtb", "worktree-bridge"):
            with self.subTest(command=name):
                command = scripts / (name + suffix)
                self.assertTrue(command.is_file(), str(command))
                status = self.run_command([str(command), "status"], installed=True)
                self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
                self.assertEqual(json.loads(status.stdout)["revision"], "legacy-revision")
                repeated = self.run_command([str(command), "init"], installed=True)
                self.assertEqual(repeated.returncode, 0, repeated.stdout + repeated.stderr)
                self.assertFalse(json.loads(repeated.stdout)["registered"])
                version = self.run_command([str(command), "--version"], installed=True)
                self.assertEqual(version.stdout.strip(), "duo 0.4.0")
        self.assert_legacy_records_unchanged()

    @unittest.skipUnless(os.environ.get("DUOLO_LEGACY_PYTHON"),
                         "Set DUOLO_LEGACY_PYTHON to an isolated v0.3 installation for live compatibility")
    def test_new_entrypoint_reuses_a_real_v03_daemon_without_restarting_or_migrating(self):
        self.server.shutdown()
        self.server.server_close()
        self.record.unlink()
        for key, value in (("user.name", "Duolo fixture"), ("user.email", "fixture@example.com"),
                           ("core.autocrlf", "false")):
            configured = self.run_command(["git", "-C", str(self.project), "config", key, value])
            self.assertEqual(configured.returncode, 0, configured.stderr)
        (self.project / "a.txt").write_text("existing pair\n", encoding="utf-8")
        commands = (["git", "-C", str(self.project), "add", "a.txt"],
                    ["git", "-C", str(self.project), "-c", "core.hooksPath=" + str(self.root / "no-hooks"),
                     "commit", "-qm", "fixture"],
                    ["git", "-c", "core.autocrlf=false", "clone", "-q", str(self.project), str(self.root / "peer")])
        for command in commands:
            prepared = self.run_command(command)
            self.assertEqual(prepared.returncode, 0, prepared.stderr)
        configuration = json.loads(self.config.read_text(encoding="utf-8"))
        configuration["remote"] = {"kind": "local", "root": str(self.root / "peer")}
        self.config.write_text(json.dumps(configuration, indent=3), encoding="utf-8")
        old_environment = dict(self.environment)
        old_environment.pop("PYTHONPATH", None)
        legacy_python = os.environ["DUOLO_LEGACY_PYTHON"]
        started = subprocess.run([legacy_python, "-m", "worktree_bridge", "--config", str(self.config), "start"],
                                 cwd=self.project, env=old_environment, capture_output=True, text=True,
                                 encoding="utf-8", timeout=35,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(started.returncode, 0, started.stdout + started.stderr)
        old_pid = json.loads(started.stdout)["pid"]
        process_handle = None
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            kernel.WaitForSingleObject.restype = wintypes.DWORD
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel.CloseHandle.restype = wintypes.BOOL
            process_handle = kernel.OpenProcess(0x100000, False, old_pid)
            self.assertTrue(process_handle, "the disposable v0.3 daemon has already exited")
        try:
            confirmed = client.wait_until_synced(self.config, timeout=20)
            self.assertEqual(confirmed["version"], "0.3.0")
            self.saved = {path: path.read_bytes() for path in (self.config, self.index, self.record)}
            for arguments in (("status",), ("init",), ("start",)):
                result = self.run_command([sys.executable, "-m", "duolo", *arguments])
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                payload = json.loads(result.stdout)
                if arguments == ("start",):
                    self.assertTrue(payload["already_running"])
                elif arguments == ("init",):
                    self.assertFalse(payload["registered"])
                else:
                    self.assertEqual(payload["version"], "0.3.0")
            self.assertEqual(client.service_info(self.config)["pid"], old_pid)
            self.assert_legacy_records_unchanged()
        finally:
            try:
                client.action(self.config, "stop")
                deadline = time.monotonic() + 10
                while self.record.exists() and time.monotonic() < deadline:
                    time.sleep(.1)
                self.assertFalse(self.record.exists(), "the disposable v0.3 daemon did not stop")
                if process_handle:
                    self.assertEqual(kernel.WaitForSingleObject(process_handle, 30000), 0,
                                     "the disposable v0.3 daemon has not exited")
                # Windows venv redirectors can retain the inherited log handle
                # briefly after the interpreter exits. Wait only on this fixture.
                log = self.state / "service.log"
                deadline = time.monotonic() + 10
                while log.exists():
                    try:
                        log.unlink()
                    except PermissionError:
                        if time.monotonic() >= deadline:
                            raise
                        time.sleep(.1)
            finally:
                if process_handle:
                    kernel.CloseHandle(process_handle)


if __name__ == "__main__":
    unittest.main()
