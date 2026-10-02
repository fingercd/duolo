import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from worktree_bridge.__main__ import Endpoint, endpoint_identity


def resolved(port=2222):
    return subprocess.CompletedProcess([], 0, f"hostname server.example\nuser developer\nport {port}\n".encode(), b"")


class SSHConfigurationTests(unittest.TestCase):
    def test_alias_port_is_not_overridden_when_omitted(self):
        response = subprocess.CompletedProcess([], 0, json.dumps({"ok": True, "result": {"sample": 1}}).encode(), b"")
        with mock.patch("subprocess.run", side_effect=[resolved(), response]) as run:
            endpoint = Endpoint({"kind": "ssh", "host": "gpu-alias", "root": "/work/repo"})
            self.assertEqual(endpoint.call("inventory"), {"sample": 1})
        config_args, call_args = [call.args[0] for call in run.call_args_list]
        self.assertIn("-G", config_args)
        self.assertNotIn("-p", config_args)
        self.assertNotIn("-p", call_args)
        self.assertEqual(endpoint.ssh_identity["port"], 2222)
        self.assertIn("StrictHostKeyChecking=yes", call_args)

    def test_explicit_port_is_used(self):
        with mock.patch("subprocess.run", return_value=resolved(12345)) as run:
            endpoint = Endpoint({"kind": "ssh", "host": "developer@server.example", "port": 12345, "root": "/work/repo"})
        args = run.call_args.args[0]
        self.assertEqual(args[args.index("-p") + 1], "12345")
        self.assertEqual(endpoint.ssh_identity["port"], 12345)

    def test_changed_alias_target_changes_endpoint_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            local = Endpoint({"kind": "local", "root": directory})
            spec = {"kind": "ssh", "host": "gpu-alias", "root": "/work/repo"}
            with mock.patch("subprocess.run", return_value=resolved(2222)):
                first = endpoint_identity(local, Endpoint(spec))
            with mock.patch("subprocess.run", return_value=resolved(3333)):
                second = endpoint_identity(local, Endpoint(spec))
        self.assertNotEqual(first, second)

    def test_invalid_host_or_port_does_not_launch_ssh(self):
        for invalid in ({"host": "-ProxyCommand=bad"}, {"host": "bad host"}, {"port": True}, {"port": 0}):
            with self.subTest(invalid=invalid), mock.patch("subprocess.run") as run:
                with self.assertRaises(ValueError):
                    Endpoint({"kind": "ssh", "host": "gpu-alias", "root": "/work/repo", **invalid})
                run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
