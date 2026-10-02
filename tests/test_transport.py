import base64
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from worktree_bridge.transport import Peer, PeerError


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        for args in (("init", "-b", "main"), ("config", "user.name", "Fixture"), ("config", "user.email", "fixture@example.invalid")):
            subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True)
        (self.root / "file.py").write_bytes(b"initial")
        subprocess.run(["git", "-C", str(self.root), "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-m", "initial"], check=True, capture_output=True)
        self.addCleanup(self.temp.cleanup)

    def peer(self, **kwargs):
        peer = Peer({"kind": "local", "root": str(self.root)}, **kwargs)
        self.addCleanup(peer.close)
        return peer

    def test_reuses_process_protocol_and_batch(self):
        peer = self.peer()
        pid = peer._process.pid
        first = peer.call("snapshot")
        self.assertEqual(peer.call("snapshot")["revision"], first["revision"])
        expected = {key: first[key] for key in ("head", "branch")}
        self.assertEqual(peer.call("write_many", actions=[{"path": "new.py", "data": "bmV3", "expected": None}], expected_git=expected)["applied"], ["new.py"])
        current = peer.call("snapshot")
        result = peer.call("read_many", paths=["new.py"], expected_git=expected, expected_hashes={"new.py": current["files"]["new.py"]})
        self.assertEqual(base64.b64decode(result["files"]["new.py"]["data"]), b"new")
        self.assertEqual(peer._process.pid, pid)
        self.assertIsNone(peer._process.poll())

    def test_operation_error_keeps_process_and_fixed_root(self):
        peer = self.peer()
        with self.assertRaisesRegex(PeerError, "unknown operation"):
            peer.call("unknown")
        self.assertIn("head", peer.call("snapshot"))
        with self.assertRaisesRegex(ValueError, "fixed"):
            peer.call("snapshot", root=str(self.root / "other"))

    def test_disconnect_is_terminal_and_does_not_replay(self):
        peer = self.peer()
        peer.call("snapshot")
        peer._process.kill()
        peer._process.wait()
        with self.assertRaises(PeerError):
            peer.call("write_many", actions=[])
        self.assertTrue(peer._closed)
        with self.assertRaisesRegex(PeerError, "closed"):
            peer.call("snapshot")

    def test_timeout_closes_peer(self):
        peer = self.peer()
        peer.call("snapshot")
        peer.timeout = 0.01
        with mock.patch.object(peer._responses, "get", side_effect=__import__("queue").Empty):
            with self.assertRaisesRegex(PeerError, "timed out"):
                peer.call("snapshot")
        self.assertTrue(peer._closed)
        self.assertIsNotNone(peer._process.poll())

    def test_invalid_protocol_is_terminal(self):
        peer = self.peer()
        peer.call("snapshot")
        peer._responses.put({"id": -1, "ok": True, "result": {}})
        with self.assertRaisesRegex(PeerError, "request id"):
            peer.call("snapshot")
        self.assertTrue(peer._closed)

    def test_ssh_alias_defaults_and_bootstrap_not_in_command(self):
        resolved = subprocess.CompletedProcess([], 0, b"hostname fixture.invalid\nuser tester\nport 2222\n", b"")
        class Pipe:
            def write(self, value):
                self.value = value
                return len(value)
            def flush(self):
                pass
            def close(self):
                pass
            def read(self, size):
                return b""
            def __iter__(self):
                return iter(())
        process = mock.Mock(stdin=Pipe(), stdout=Pipe(), stderr=Pipe())
        process.poll.return_value = 0
        with mock.patch("subprocess.run", return_value=resolved), mock.patch("subprocess.Popen", return_value=process) as popen:
            peer = Peer({"kind": "ssh", "root": "/fixture/repo", "host": "fixture-alias"})
            command = popen.call_args.args[0]
            self.assertNotIn("-p", command)
            self.assertIn("StrictHostKeyChecking=yes", command)
            self.assertLess(len(command[-1]), 2000)
            self.assertEqual(peer.ssh_identity["port"], 2222)
            if sys.platform == "win32":
                self.assertEqual(popen.call_args.kwargs["creationflags"], subprocess.CREATE_NO_WINDOW)
            peer.close()

    def test_remote_style_controller_lease_excludes_second_writer(self):
        first = self.peer()
        second = self.peer()
        self.assertTrue(first.call("claim_controller", instance_id="first-controller")["claimed"])
        snap = second.call("snapshot")
        expected = {key: snap[key] for key in ("head", "branch")}
        with self.assertRaisesRegex(PeerError, "lease held"):
            second.call("claim_controller", instance_id="second-controller")
        with self.assertRaisesRegex(PeerError, "lease held"):
            second.call("write_many", actions=[{"path": "blocked.py", "data": "YQ==", "expected": None}], expected_git=expected)
        self.assertFalse((self.root / "blocked.py").exists())
        self.assertEqual(second.call("snapshot")["head"], snap["head"])
        first.close()
        self.assertTrue(second.call("claim_controller", instance_id="second-controller")["claimed"])
        second.call("write_many", actions=[{"path": "allowed.py", "data": "YQ==", "expected": None}], expected_git=expected)
        self.assertEqual((self.root / "allowed.py").read_bytes(), b"a")

    def test_large_jsonl_payload(self):
        peer = self.peer()
        snap = peer.call("snapshot")
        expected = {key: snap[key] for key in ("head", "branch")}
        data = b"x" * (1024 * 1024)
        peer.call("write_many", actions=[{"path": "large.py", "data": base64.b64encode(data).decode(), "expected": None}], expected_git=expected)
        snap = peer.call("snapshot")
        result = peer.call("read_many", paths=["large.py"], expected_git=expected, expected_hashes={"large.py": snap["files"]["large.py"]})
        self.assertEqual(base64.b64decode(result["files"]["large.py"]["data"]), data)


if __name__ == "__main__":
    unittest.main()
