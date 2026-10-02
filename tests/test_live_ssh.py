"""Opt-in Linux SSH tests, restricted to disposable GitHub runner fixtures.

Default unittest discovery does not connect to SSH. The dedicated CI job sets
WTB_TEST_SSH_HOST, WTB_TEST_SSH_PORT and WTB_TEST_SSH_ROOT and supplies its own
loopback sshd, keys and SSH alias; no existing project or user key is used.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from worktree_bridge.__main__ import Endpoint
from worktree_bridge.service import BridgeService
from worktree_bridge.transport import Peer, PeerError


def digest(data):
    return hashlib.sha256(data).hexdigest()


@unittest.skipUnless(os.environ.get("WTB_TEST_SSH_HOST"), "live SSH disabled; dedicated CI environment required")
class LiveSSHTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not sys.platform.startswith("linux") or os.environ.get("GITHUB_ACTIONS") != "true":
            raise RuntimeError("live SSH fixtures are restricted to disposable Linux GitHub runners")
        cls.host = os.environ["WTB_TEST_SSH_HOST"]
        cls.port = int(os.environ["WTB_TEST_SSH_PORT"])
        cls.fixture_parent = Path(os.environ["WTB_TEST_SSH_ROOT"])
        runner_temp = Path(os.environ["RUNNER_TEMP"]).resolve()
        if (not cls.fixture_parent.is_absolute() or cls.fixture_parent.is_symlink()
                or runner_temp not in cls.fixture_parent.resolve().parents
                or cls.fixture_parent.name != "wtb-ssh-fixtures"):
            raise RuntimeError("WTB_TEST_SSH_ROOT must be a dedicated wtb-ssh-fixtures directory under RUNNER_TEMP")
        if not cls.fixture_parent.is_dir():
            raise RuntimeError("CI must create its owned fixture parent before enabling live SSH")
        endpoint = Endpoint({"kind": "ssh", "host": cls.host, "port": cls.port,
                             "root": str(cls.fixture_parent)})
        if endpoint.ssh_identity["hostname"] != "127.0.0.1" or endpoint.ssh_identity["port"] != cls.port:
            raise RuntimeError("live test SSH alias must resolve to its explicit loopback port")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wtb-ci-live-", dir=str(self.fixture_parent))
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.local = self.root / "local"
        self.remote = self.root / "remote"
        self.local.mkdir()
        self.git(self.local, "init", "-q", "-b", "main")
        self.git(self.local, "config", "user.name", "SSH CI Fixture")
        self.git(self.local, "config", "user.email", "fixture@example.invalid")
        self.git(self.local, "config", "core.autocrlf", "false")
        (self.local / "code.py").write_bytes(b"base\n")
        (self.local / "AGENTS.md").write_bytes(b"Disposable SSH protocol fixture.\n")
        (self.local / ".gitignore").write_bytes(b"ignored/\n")
        self.git(self.local, "add", ".")
        self.git(self.local, "commit", "-qm", "isolated fixture")
        self.git(self.root, "clone", "-q", "--no-hardlinks", str(self.local), str(self.remote))
        self.git(self.remote, "config", "core.autocrlf", "false")
        self.spec = {"kind": "ssh", "host": self.host, "port": self.port, "root": str(self.remote)}

    @staticmethod
    def git(root, *args):
        return subprocess.run(["git", "-C", str(root), *args], check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20).stdout

    def peer(self, spec):
        peer = Peer(spec, timeout=20, reconcile_interval=300)
        self.addCleanup(peer.close)
        return peer

    def service(self):
        config_path = self.root / "config.json"
        config_path.write_text(json.dumps({
            "local_root": str(self.local), "remote": self.spec,
            "state_dir": str(self.root / "state"),
            "service": {"poll_interval": .05, "reconcile_interval": 30,
                        "auto_sync": True, "auto_git": False, "allow_delete": False}
        }), encoding="utf-8")
        service = BridgeService(config_path).start()
        def close():
            service.stop()
            service.join(15)
            self.assertFalse(service._thread.is_alive(), "controller did not release owned SSH peers")
        self.addCleanup(close)
        return service

    def wait(self, service, predicate, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            view = service.view()
            if predicate(view):
                return view
            if view["state"] in ("error", "git_blocked"):
                self.fail("unexpected controller state: " + json.dumps(view, sort_keys=True))
            time.sleep(.05)
        self.fail("controller condition timed out: " + json.dumps(service.view(), sort_keys=True))

    def pause(self, service):
        self.assertTrue(service.action("pause")["ok"])
        self.wait(service, lambda view: view["state"] == "paused")

    def test_real_jsonl_native_linux_watch_and_batch_roundtrip(self):
        # An empty directory is absent from cached Git selection. Its new file
        # must be discovered by inotify hints before the 300-second reconcile.
        empty = self.remote / "previously-empty"
        empty.mkdir()
        remote = self.peer(self.spec)
        local = self.peer({"kind": "local", "root": str(self.local)})
        first = remote.call("snapshot")
        self.assertEqual(first["watcher"], "inotify")
        pid = remote._process.pid
        self.assertEqual(remote.call("snapshot")["revision"], first["revision"])
        (empty / "new.py").write_bytes(b"native event\n")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if "previously-empty/new.py" in remote.call("snapshot")["files"]:
                break
            time.sleep(.05)
        else:
            self.fail("inotify did not reveal a file added under an uncached directory")
        expected = {key: first[key] for key in ("head", "branch")}
        payload = b"written over actual SSH JSONL\n"
        remote.call("write_many", expected_git=expected,
                    actions=[{"path": "code.py", "data": base64.b64encode(payload).decode("ascii"),
                              "expected": first["files"]["code.py"]}])
        received = remote.call("read_many", expected_git=expected, paths=["code.py"],
                               expected_hashes={"code.py": digest(payload)})["files"]["code.py"]
        self.assertEqual(base64.b64decode(received["data"]), payload)
        local_snap = local.call("snapshot")
        local.call("write_many", expected_git=expected,
                   actions=[{"path": "code.py", "data": received["data"],
                             "expected": local_snap["files"]["code.py"]}])
        self.assertEqual((self.local / "code.py").read_bytes(), payload)
        self.assertEqual((self.remote / "code.py").read_bytes(), payload)
        with self.assertRaisesRegex(PeerError, "destination changed"):
            remote.call("write_many", expected_git=expected,
                        actions=[{"path": "code.py", "data": "c3RhbGU=", "expected": first["files"]["code.py"]}])
        self.assertEqual((self.remote / "code.py").read_bytes(), payload)
        self.assertEqual(remote._process.pid, pid)

    def test_controller_syncs_both_directions_and_stops_entire_conflicting_batch(self):
        service = self.service()
        view = self.wait(service, lambda item: item["state"] == "synced")
        self.assertTrue(all(connection["connected"] for connection in view["connection"].values()))
        (self.local / "code.py").write_bytes(b"local save\n")
        self.wait(service, lambda item: item["state"] == "synced"
                  and (self.remote / "code.py").read_bytes() == b"local save\n")
        (self.remote / "code.py").write_bytes(b"remote save\n")
        self.wait(service, lambda item: item["state"] == "synced"
                  and (self.local / "code.py").read_bytes() == b"remote save\n")
        self.pause(service)
        (self.local / "code.py").write_bytes(b"independent local\n")
        (self.remote / "code.py").write_bytes(b"independent remote\n")
        (self.local / "pending.py").write_bytes(b"must not cross conflict\n")
        self.assertTrue(service.action("resume")["ok"])
        view = self.wait(service, lambda item: item["state"] == "conflict")
        self.assertIn("code.py", {conflict["path"] for conflict in view["conflicts"]})
        self.assertEqual((self.local / "code.py").read_bytes(), b"independent local\n")
        self.assertEqual((self.remote / "code.py").read_bytes(), b"independent remote\n")
        self.assertFalse((self.remote / "pending.py").exists())

    def test_controller_reconnects_after_owned_ssh_process_disconnect_without_losing_edits(self):
        service = self.service()
        self.wait(service, lambda view: view["state"] == "synced")
        self.pause(service)
        old_peer = service._peers["remote"]
        old_peer._process.kill()
        old_peer._process.wait(timeout=5)
        (self.local / "code.py").write_bytes(b"saved during SSH disconnection\n")
        self.assertTrue(service.action("resume")["ok"])
        view = self.wait(service, lambda item: item["state"] == "synced"
                         and service._peers.get("remote") is not old_peer
                         and (self.remote / "code.py").read_bytes() == b"saved during SSH disconnection\n")
        self.assertTrue(view["connection"]["remote"]["connected"])
        self.assertEqual((self.local / "code.py").read_bytes(), b"saved during SSH disconnection\n")
        self.assertTrue((self.root / "state/baseline.json").is_file())


if __name__ == "__main__":
    unittest.main()
