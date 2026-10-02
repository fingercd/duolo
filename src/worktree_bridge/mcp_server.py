"""Optional official MCP SDK adapter for one configured local bridge daemon.

Importing this module needs only the standard library. The SDK and daemon client
are loaded when a server is created, so the daemon does not depend on MCP.
"""

import asyncio
import json
import math
from pathlib import Path
from typing import Annotated, Any, Literal


class MCPDependencyError(RuntimeError):
    """The optional SDK is not installed in this Python environment."""


class _BoundProject:
    """Keep daemon identity and authorization in the existing local client."""

    def __init__(self, config_path, client_module):
        self.config_path = Path(config_path).resolve()
        self.client = client_module

    def _invoke(self, function, *args):
        try:
            return function(self.config_path, *args)
        except self.client.WaitTimeout as exc:
            result = dict(exc.last_status)
            result.update(ok=False, synced=False, timed_out=True)
            result["error"] = {"type": "WaitTimeout", "message": str(exc)}
            return result
        except self.client.ClientError as exc:
            return {
                "ok": False,
                "state": "offline" if isinstance(exc, self.client.ServiceUnavailable) else "error",
                "synced": False,
                "error": {"type": type(exc).__name__, "message": str(exc)},
            }

    def project_status(self):
        return self._invoke(self.client.get_status)

    def action(self, name, params):
        return self._invoke(self.client.action, name, params)

    def list_conflicts(self):
        status = self.project_status()
        if status.get("ok") is False:
            return status
        return {key: status[key] for key in
                ("schema", "name", "state", "revision", "updated_at", "connection", "conflicts")
                if key in status}

    def wait_until_synced(self, timeout):
        if not math.isfinite(timeout) or timeout < 0:
            return {"ok": False, "synced": False,
                    "error": {"type": "InvalidTimeout", "message": "timeout must be finite and non-negative"}}
        return self._invoke(self.client.wait_until_synced, timeout)


def create_server(config_path):
    """Create a FastMCP server bound to config_path; no daemon is auto-started."""
    try:
        from mcp.server.fastmcp import FastMCP
        from mcp.types import CallToolResult, TextContent, ToolAnnotations
    except ImportError as exc:
        raise MCPDependencyError(
            'MCP requires the optional official SDK. Install it with '
            'python -m pip install ".[mcp]" from the project checkout '
            '(SDK constraint: mcp>=1.28,<2).'
        ) from exc

    from . import client

    project = _BoundProject(config_path, client)
    server = FastMCP(
        "Worktree Bridge",
        instructions=(
            "This server controls only the project fixed by its startup configuration. "
            "Status is the daemon's cached observation: inspect updated_at, connection, "
            "state, and last_error; it is not a new full filesystem scan. "
            "Write tools request guarded daemon actions. queued=true means accepted, "
            "not completed. Use wait_until_synced or project_status to observe completion. "
            "Conflict resolution can overwrite the other side; checkpoint creates Git commits "
            "and may create a tag. No shell, arbitrary URL, or alternate root is exposed."
        ),
    )

    async def invoke(function, *args):
        payload = await asyncio.to_thread(function, *args)
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))],
            structuredContent=payload,
            isError=payload.get("ok") is False,
        )

    def register(function, *, read_only=False, destructive=False, idempotent=False):
        # Assign the resolved SDK annotation after lazy import. FastMCP uses it
        # to publish and validate the output schema without a global SDK import.
        function.__annotations__["return"] = Annotated[CallToolResult, dict[str, Any]]
        server.tool(annotations=ToolAnnotations(
            readOnlyHint=read_only,
            destructiveHint=destructive,
            idempotentHint=idempotent,
            openWorldHint=False,
        ))(function)

    async def project_status():
        """Read cached daemon state with its observation times; no full scan is implied."""
        return await invoke(project.project_status)

    async def sync_now():
        """Request guarded reconciliation and writes for the configured pair; receipt may be queued."""
        return await invoke(project.action, "sync", {})

    async def pause_sync():
        """Pause automatic synchronization for this project; an in-flight action may finish."""
        return await invoke(project.action, "pause", {})

    async def resume_sync():
        """Resume automatic synchronization; guarded file writes and configured Git following may occur."""
        return await invoke(project.action, "resume", {})

    async def list_conflicts():
        """Read cached conflicts and revision for resolving a configured project file."""
        return await invoke(project.list_conflicts)

    async def resolve_conflict(path: str, choice: Literal["local", "remote"], expected_revision: str):
        """Choose a side for a relative conflict path, overwriting the other side only if revision still matches."""
        return await invoke(project.action, "resolve", {
            "path": path, "choice": choice, "expected_revision": expected_revision,
        })

    async def wait_until_synced(timeout: float = 30.0):
        """Request a fresh observation barrier and wait for synced state; never resumes paused synchronization."""
        return await invoke(project.wait_until_synced, timeout)

    async def checkpoint(message: str, tag: str | None = None):
        """Create a guarded Git checkpoint commit for eligible changes and optionally a tag; peer following may be queued."""
        params = {"message": message}
        if tag is not None:
            params["tag"] = tag
        return await invoke(project.action, "checkpoint", params)

    register(project_status, read_only=True, idempotent=True)
    register(sync_now, destructive=True)
    register(pause_sync, idempotent=True)
    register(resume_sync, destructive=True, idempotent=True)
    register(list_conflicts, read_only=True, idempotent=True)
    register(resolve_conflict, destructive=True)
    register(wait_until_synced, read_only=True, idempotent=True)
    register(checkpoint, destructive=True)
    return server


def run(config_path):
    """Run the official SDK stdio transport; stdout is reserved for MCP."""
    create_server(config_path).run(transport="stdio")


def main():
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="MCP stdio adapter for one local Worktree Bridge daemon")
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    try:
        run(args.config)
    except MCPDependencyError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
