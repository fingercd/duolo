import asyncio
import builtins
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import types
import unittest
from unittest import mock


SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))
from duolo import mcp_server


class ClientError(RuntimeError):
    pass


class ServiceUnavailable(ClientError):
    pass


class WaitTimeout(ClientError):
    def __init__(self, status):
        self.last_status = status
        super().__init__("project did not synchronize before timeout")


class BoundProjectTests(unittest.TestCase):
    def setUp(self):
        self.status = {
            "schema": 1, "name": "fixture", "state": "conflict", "revision": "r1",
            "updated_at": 123.0, "connection": {"remote": {"connected": False, "last_seen": 100.0}},
            "conflicts": [{"path": "a.txt", "reason": "both changed"}],
            "last_error": "remote scan failed",
        }
        self.client = types.SimpleNamespace(
            ClientError=ClientError, ServiceUnavailable=ServiceUnavailable, WaitTimeout=WaitTimeout,
            get_status=mock.Mock(return_value=self.status),
            action=mock.Mock(return_value={"queued": True}),
            wait_until_synced=mock.Mock(return_value={"state": "synced"}),
        )
        self.config = Path("fixture-config.json").resolve()
        self.project = mcp_server._BoundProject(self.config, self.client)

    def test_status_preserves_cached_observation_and_failure(self):
        self.assertEqual(self.project.project_status(), self.status)
        self.client.get_status.assert_called_once_with(self.config)

    def test_actions_are_bound_and_preserve_queued_receipt(self):
        self.assertEqual(self.project.action("sync", {}), {"queued": True})
        self.client.action.assert_called_once_with(self.config, "sync", {})

    def test_conflicts_include_revision_and_observation_time(self):
        result = self.project.list_conflicts()
        self.assertEqual(result["conflicts"], self.status["conflicts"])
        self.assertEqual(result["revision"], "r1")
        self.assertEqual(result["updated_at"], 123.0)
        self.assertEqual(result["connection"], self.status["connection"])

    def test_timeout_returns_last_state_without_claiming_success(self):
        self.client.wait_until_synced.side_effect = WaitTimeout(self.status)
        result = self.project.wait_until_synced(0.1)
        self.assertFalse(result["ok"])
        self.assertFalse(result["synced"])
        self.assertTrue(result["timed_out"])
        self.assertEqual(result["state"], "conflict")
        self.assertEqual(result["revision"], "r1")
        self.assertNotIn("timed_out", self.status)
        self.client.wait_until_synced.assert_called_once_with(self.config, 0.1)

    def test_offline_is_structured_and_actions_are_not_retried(self):
        self.client.action.side_effect = ServiceUnavailable("daemon is offline; result may be unknown")
        result = self.project.action("checkpoint", {"message": "save"})
        self.assertEqual(result["state"], "offline")
        self.assertFalse(result["synced"])
        self.assertEqual(result["error"]["type"], "ServiceUnavailable")
        self.client.action.assert_called_once()

    def test_guard_failure_remains_failure_and_is_not_offline(self):
        self.client.action.side_effect = ClientError("stale expected_revision")
        result = self.project.action("resolve", {"expected_revision": "old"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["state"], "error")
        self.assertIn("stale", result["error"]["message"])

    def test_invalid_wait_does_not_call_daemon(self):
        for timeout in (-1, float("inf"), float("nan")):
            with self.subTest(timeout=timeout):
                result = self.project.wait_until_synced(timeout)
                self.assertFalse(result["ok"])
                self.assertFalse(result["synced"])
        self.client.wait_until_synced.assert_not_called()

    def test_missing_optional_dependency_gives_install_command(self):
        real_import = builtins.__import__

        def without_mcp(name, *args, **kwargs):
            if name == "mcp" or name.startswith("mcp."):
                raise ModuleNotFoundError("No module named 'mcp'", name="mcp")
            return real_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__", side_effect=without_mcp):
            with self.assertRaisesRegex(mcp_server.MCPDependencyError, r"\.\[mcp\]"):
                mcp_server.create_server(self.config)

    def test_import_does_not_load_sdk(self):
        env = dict(os.environ, PYTHONPATH=str(SRC))
        result = subprocess.run(
            [sys.executable, "-c", "import sys; import duolo.mcp_server; assert 'mcp' not in sys.modules"],
            env=env, capture_output=True, text=True, encoding="utf-8", timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")


# This SDK client launches a real stdio server subprocess. The daemon client is
# a fixture: no real SSH endpoint, filesystem synchronization, or Git write occurs.
@unittest.skipUnless(importlib.util.find_spec("mcp"),
                     'official MCP SDK not installed; install .[mcp] to test real stdio')
class MCPStdioTests(unittest.TestCase):
    def test_official_client_handshake_tools_and_guarded_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "fixture_server.py"
            script.write_text(textwrap.dedent('''
                import sys
                import types
                import duolo
                from duolo.mcp_server import run

                class ClientError(RuntimeError): pass
                class ServiceUnavailable(ClientError): pass
                class WaitTimeout(ClientError):
                    def __init__(self, status):
                        self.last_status = status
                        super().__init__("timeout")

                state = {"schema": 1, "name": "stdio fixture", "state": "conflict",
                         "revision": "r1", "updated_at": 123.0, "connection": {},
                         "conflicts": [{"path": "a.txt", "reason": "both changed"}]}
                def get_status(config):
                    return dict(state)
                def action(config, name, params):
                    if name == "resolve" and params["expected_revision"] != state["revision"]:
                        raise ClientError("stale revision")
                    if name == "resolve" and params["path"] != "a.txt":
                        raise ClientError("path outside configured selection")
                    if name == "sync":
                        state["state"] = "synced"
                    return {"queued": True, "action": name, "params": params,
                            "config_name": config.name}
                def wait_until_synced(config, timeout):
                    if state["state"] != "synced":
                        raise WaitTimeout(dict(state))
                    return dict(state)
                client = types.ModuleType("duolo.client")
                for name in ("ClientError", "ServiceUnavailable", "WaitTimeout",
                             "get_status", "action", "wait_until_synced"):
                    setattr(client, name, globals()[name])
                sys.modules[client.__name__] = client
                duolo.client = client
                run(sys.argv[1])
            '''), encoding="utf-8")
            asyncio.run(self._exercise_stdio(script, Path(directory) / "bound-config.json"))

    async def _exercise_stdio(self, script, config):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        parameters = StdioServerParameters(
            command=sys.executable, args=[str(script), str(config)],
            env={"PYTHONPATH": str(SRC)},
        )
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as session:
                initialized = await session.initialize()
                self.assertTrue(initialized.protocolVersion)
                self.assertEqual(initialized.serverInfo.name, "Duolo")
                listed = await session.list_tools()
                tools = {tool.name: tool for tool in listed.tools}
                self.assertEqual(set(tools), {
                    "project_status", "sync_now", "pause_sync", "resume_sync",
                    "list_conflicts", "resolve_conflict", "wait_until_synced", "checkpoint",
                })
                for tool in tools.values():
                    self.assertIsNotNone(tool.outputSchema)
                    fields = set(tool.inputSchema.get("properties", {}))
                    self.assertFalse(fields & {"config", "config_path", "url", "root", "shell", "command"})
                self.assertTrue(tools["project_status"].annotations.readOnlyHint)
                self.assertTrue(tools["resolve_conflict"].annotations.destructiveHint)
                self.assertTrue(tools["checkpoint"].annotations.destructiveHint)

                status = await session.call_tool("project_status", {})
                self.assertFalse(status.isError)
                self.assertEqual(status.structuredContent["updated_at"], 123.0)
                conflicts = await session.call_tool("list_conflicts", {})
                self.assertEqual(conflicts.structuredContent["revision"], "r1")
                timeout = await session.call_tool("wait_until_synced", {"timeout": 0})
                self.assertTrue(timeout.isError)
                self.assertFalse(timeout.structuredContent["synced"])
                self.assertEqual(timeout.structuredContent["state"], "conflict")

                rejected = await session.call_tool("resolve_conflict", {
                    "path": "a.txt", "choice": "local", "expected_revision": "old",
                })
                self.assertTrue(rejected.isError)
                self.assertIn("stale", rejected.structuredContent["error"]["message"])
                outside = await session.call_tool("resolve_conflict", {
                    "path": "../escape", "choice": "local", "expected_revision": "r1",
                })
                self.assertTrue(outside.isError)
                chosen = await session.call_tool("resolve_conflict", {
                    "path": "a.txt", "choice": "remote", "expected_revision": "r1",
                })
                self.assertFalse(chosen.isError)
                self.assertEqual(chosen.structuredContent["params"]["choice"], "remote")
                invalid = await session.call_tool("resolve_conflict", {
                    "path": "a.txt", "choice": "shell", "expected_revision": "r1",
                })
                self.assertTrue(invalid.isError)

                for tool, name in (("pause_sync", "pause"), ("resume_sync", "resume"), ("sync_now", "sync")):
                    receipt = await session.call_tool(tool, {})
                    self.assertFalse(receipt.isError)
                    self.assertTrue(receipt.structuredContent["queued"])
                    self.assertEqual(receipt.structuredContent["action"], name)
                    self.assertEqual(receipt.structuredContent["config_name"], config.name)
                synced = await session.call_tool("wait_until_synced", {"timeout": 1})
                self.assertFalse(synced.isError)
                self.assertEqual(synced.structuredContent["state"], "synced")
                checkpoint = await session.call_tool("checkpoint", {"message": "test commit", "tag": "experiment-1"})
                self.assertEqual(checkpoint.structuredContent["params"], {
                    "message": "test commit", "tag": "experiment-1",
                })
                plain = await session.call_tool("checkpoint", {"message": "test commit"})
                self.assertEqual(plain.structuredContent["params"], {"message": "test commit"})
                # Every content block is valid JSON: an accidental stdout print
                # would break the official client earlier in this handshake.
                self.assertEqual(json.loads(status.content[0].text), status.structuredContent)


if __name__ == "__main__":
    unittest.main()
