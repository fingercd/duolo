"""Explicit SSH write acceptance, confined to disposable wtb-acceptance fixtures.

Run manually: python scripts/ssh_acceptance.py --config PRIVATE.json --report PRIVATE.json
The config uses bridge's normal fields plus ``acceptance``: fixture_id,
expected_head, expected_branch, marker_sha256, and initial_hashes for AGENTS.md
and CONTEXT.md. Both roots and state_dir must start with wtb-acceptance-.
Fixtures must be clean, identical Git checkouts on branch wtb-acceptance with
a tracked .wtb-acceptance.json containing schema=1, purpose=wtb-acceptance,
and the configured fixture_id. Config, state, and report stay outside this source
tree and the fixture. No cleanup, retry of writes, or disconnect injection occurs.
"""

import argparse
import base64
import hashlib
import inspect
import json
from pathlib import Path, PurePosixPath
import re
import shlex
import subprocess
import sys

SOURCE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE_ROOT / "src"))
from duolo import __main__ as bridge

DOCUMENTS = ("AGENTS.md", "CONTEXT.md")
MARKER = ".wtb-acceptance.json"


def fixture_operation(request):
    """Self-contained Python 3.9 program used identically on either host."""
    import base64
    import hashlib
    import json
    import os
    from pathlib import Path
    import stat
    import subprocess
    import tempfile

    root = Path(request["root"])
    contract = request["acceptance"]
    if not root.is_absolute() or root.resolve(strict=True) != root:
        raise ValueError("fixture root must be canonical and cannot traverse symlinks")
    if not root.name.startswith("wtb-acceptance-"):
        raise ValueError("fixture root basename must start with wtb-acceptance-")

    def git(*args):
        proc = subprocess.run(["git", "-C", str(root), *args], check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              env={key: value for key, value in os.environ.items()
                                   if not key.upper().startswith("GIT_")}, timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return proc.stdout.decode("utf-8").strip()

    if Path(git("rev-parse", "--show-toplevel")).resolve() != root:
        raise ValueError("fixture root is not the Git worktree root")
    head = git("rev-parse", "HEAD")
    branch = git("symbolic-ref", "--short", "HEAD")
    if head != contract["expected_head"] or branch != contract["expected_branch"]:
        raise ValueError("fixture Git HEAD or branch differs from acceptance contract")
    payloads = {}
    hashes = {}
    for name in (".wtb-acceptance.json", "AGENTS.md", "CONTEXT.md"):
        path = root / name
        if not stat.S_ISREG(path.lstat().st_mode):
            raise ValueError("fixture file must be regular: " + name)
        git("ls-files", "--error-unmatch", "--", name)
        data = path.read_bytes()
        if len(data) > 1024 * 1024:
            raise ValueError("fixture file too large: " + name)
        payloads[name] = base64.b64encode(data).decode("ascii")
        hashes[name] = hashlib.sha256(data).hexdigest()
    if hashes[".wtb-acceptance.json"] != contract["marker_sha256"]:
        raise ValueError("fixture marker hash differs")
    marker = json.loads(base64.b64decode(payloads[".wtb-acceptance.json"]))
    if (marker.get("schema") != 1 or marker.get("purpose") != "wtb-acceptance"
            or marker.get("fixture_id") != contract["fixture_id"]):
        raise ValueError("fixture marker identity differs")
    if request.get("initial"):
        if git("status", "--porcelain"):
            raise ValueError("initial fixture must have clean Git status")
        for name, expected in contract["initial_hashes"].items():
            if hashes[name] != expected:
                raise ValueError("initial fixture content hash differs: " + name)
    if request.get("op") == "write":
        name = request["path"]
        if name not in ("AGENTS.md", "CONTEXT.md"):
            raise ValueError("acceptance injection may only edit AGENTS.md or CONTEXT.md")
        if hashes[name] != request["expected"]:
            raise ValueError("fixture changed before acceptance injection: " + name)
        data = base64.b64decode(request["data"], validate=True)
        if len(data) > 1024 * 1024:
            raise ValueError("acceptance injection is too large")
        fd, temporary = tempfile.mkstemp(prefix=".wtb-acceptance-write-", dir=str(root))
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
            target = root / name
            if (not stat.S_ISREG(target.lstat().st_mode)
                    or hashlib.sha256(target.read_bytes()).hexdigest() != request["expected"]):
                raise ValueError("fixture changed during acceptance injection: " + name)
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        hashes[name] = hashlib.sha256(data).hexdigest()
        payloads[name] = base64.b64encode(data).decode("ascii")
    elif request.get("op") != "inspect":
        raise ValueError("unsupported acceptance operation")
    return {"root": str(root), "head": head, "branch": branch,
            "hashes": hashes, "payloads": payloads}


def inside(path, root):
    return path == root or root in path.parents


def validate_config(config_path, report_path):
    config_path, report_path = Path(config_path), Path(report_path)
    if not config_path.is_absolute() or not report_path.is_absolute():
        raise ValueError("config and report paths must be absolute")
    config_path, report_path = config_path.resolve(), report_path.resolve()
    config = json.loads(config_path.read_text(encoding="utf-8"))
    local, remote, state = bridge.configuration(config_path)
    if remote.spec["kind"] != "ssh":
        raise ValueError("SSH acceptance requires remote.kind=ssh")
    spec = remote.spec
    if (not isinstance(spec.get("host"), str) or not spec["host"]
            or spec["host"].startswith("-") or any(c.isspace() for c in spec["host"])):
        raise ValueError("invalid SSH host")
    if type(spec.get("port", 22)) is not int or not 1 <= spec.get("port", 22) <= 65535:
        raise ValueError("invalid SSH port")
    remote_root = PurePosixPath(remote.root)
    if (not remote_root.is_absolute() or ".." in remote_root.parts
            or str(remote_root) != remote.root):
        raise ValueError("remote fixture root must be a canonical absolute POSIX path")
    for name in (Path(local.root).name, remote_root.name, state.name):
        if not name.startswith("wtb-acceptance-"):
            raise ValueError("fixture roots and state basename must start with wtb-acceptance-")
    contract = config["acceptance"]
    if not re.fullmatch(r"wtb-acceptance-[A-Za-z0-9_-]+", contract["fixture_id"]):
        raise ValueError("invalid acceptance fixture_id")
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", contract["expected_head"]):
        raise ValueError("invalid expected Git HEAD")
    if contract["expected_branch"] != "wtb-acceptance":
        raise ValueError("acceptance branch must be wtb-acceptance")
    if set(contract["initial_hashes"]) != set(DOCUMENTS):
        raise ValueError("initial_hashes must contain exactly AGENTS.md and CONTEXT.md")
    for value in [contract["marker_sha256"], *contract["initial_hashes"].values()]:
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError("invalid acceptance SHA256")
    for private in (config_path, state, report_path):
        if inside(private, SOURCE_ROOT) or inside(private, Path(local.root).resolve()):
            raise ValueError("config, state and report must be outside source and local fixture")
    if report_path == config_path or inside(report_path, state):
        raise ValueError("report must be separate from config and state")
    if report_path.exists():
        raise ValueError("report already exists; choose a fresh report path")
    if state.exists() and any(state.iterdir()):
        raise ValueError("acceptance state directory must be fresh and empty")
    return config, local, remote, state, report_path


class Acceptance:
    def __init__(self, config_path, report_path):
        self.config_path = str(config_path)
        self.config, self.local, self.remote, self.state, self.report_path = validate_config(
            config_path, report_path)
        self.endpoints = {"local": self.local, "remote": self.remote}
        self.stage = "preflight"
        self.report = {"schema": 1, "ok": False, "fixture_id": self.config["acceptance"]["fixture_id"],
                       "steps": [], "write_retry": False, "fixtures_retained": True}
        self.original = {}

    def fixture(self, side, **kwargs):
        endpoint = self.endpoints[side]
        request = {"root": endpoint.root, "acceptance": self.config["acceptance"],
                   "op": "inspect", **kwargs}
        if side == "local":
            return fixture_operation(request)
        program = inspect.getsource(fixture_operation) + (
            "\nimport json,sys\n"
            "try:\n print(json.dumps({'ok':True,'result':fixture_operation(json.load(sys.stdin))}))\n"
            "except Exception as exc:\n print(json.dumps({'ok':False,'error':str(exc)}));sys.exit(2)\n")
        encoded = base64.b64encode(program.encode("utf-8")).decode("ascii")
        code = "import base64;exec(base64.b64decode('" + encoded + "'))"
        command = [*endpoint.ssh_args, "python3 -c " + shlex.quote(code)]
        proc = subprocess.run(
            command,
            input=json.dumps(request).encode("utf-8"), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=45, check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if proc.returncode:
            raise RuntimeError("acceptance SSH failed (write outcome may be unknown): "
                               + proc.stderr.decode("utf-8", "replace") + " "
                               + proc.stdout.decode("utf-8", "replace"))
        response = json.loads(proc.stdout)
        if not response["ok"]:
            raise RuntimeError(response["error"])
        return response["result"]

    def inject(self, side, path, data):
        current = self.fixture(side)
        return self.fixture(side, op="write", path=path, expected=current["hashes"][path],
                            data=base64.b64encode(data).decode("ascii"))

    def cli(self, command, **kwargs):
        args = argparse.Namespace(config=self.config_path, command=command,
                                  refresh=False, out=None, plan=None)
        for key, value in kwargs.items():
            setattr(args, key, value)
        return bridge.run(args)

    def evidence(self):
        return {side: {key: snapshot[key] for key in ("head", "branch", "files")}
                for side, snapshot in bridge.snapshots(self.local, self.remote).items()}

    def save(self):
        bridge.save_json(self.report_path, self.report)

    def step(self, name, operation):
        self.stage = name
        detail = operation()
        self.report["steps"].append({"name": name, "status": "passed",
                                     "detail": detail, "content_hashes": self.evidence()})
        self.save()

    def assert_equal(self):
        evidence = self.evidence()
        if evidence["local"] != evidence["remote"]:
            raise AssertionError("fixture inventories differ")
        return evidence

    def transfer(self, side, path, data, name):
        self.inject(side, path, data)
        plan_path = self.state / (name + ".json")
        result = self.cli("plan", out=str(plan_path))
        actions = result["plan"]["actions"]
        if (not result["ok"] or len(actions) != 1 or actions[0]["path"] != path
                or actions[0]["source"] != side):
            raise AssertionError("unexpected transfer plan: " + bridge.canonical(result))
        applied = self.cli("apply", plan=str(plan_path))
        self.assert_equal()
        return {"plan_id": result["plan"]["id"], "apply": applied}

    def rejected_apply(self, plan_path, expected_error):
        before = self.evidence()
        try:
            result = self.cli("apply", plan=str(plan_path))
        except ValueError as exc:
            error = str(exc)
            if expected_error not in error:
                raise AssertionError("unexpected apply rejection: " + error) from exc
        else:
            raise AssertionError("apply unexpectedly succeeded: " + bridge.canonical(result))
        if self.evidence() != before:
            raise AssertionError("rejected apply changed fixture content")
        return {"error": error, "unchanged": True}

    def preflight(self):
        for side in self.endpoints:
            result = self.fixture(side, initial=True)
            self.original[side] = {name: base64.b64decode(result["payloads"][name])
                                   for name in DOCUMENTS}
        self.assert_equal()
        return {"git_head": self.config["acceptance"]["expected_head"],
                "marker_sha256": self.config["acceptance"]["marker_sha256"]}

    def conflict(self):
        self.inject("local", "AGENTS.md", self.shared_agents + b"local conflict\n")
        self.inject("remote", "AGENTS.md", self.shared_agents + b"remote conflict\n")
        path = self.state / "conflict.json"
        before = self.evidence()
        planned = self.cli("plan", out=str(path))
        if planned["ok"] or not any(item.get("reason") == "conflict"
                and item.get("path") == "AGENTS.md" for item in planned["plan"]["blockers"]):
            raise AssertionError("conflict plan was not rejected")
        if self.evidence() != before:
            raise AssertionError("conflict plan changed content")
        return {"blockers": planned["plan"]["blockers"],
                "apply": self.rejected_apply(path, "plan has blockers")}

    def stale(self):
        for side in self.endpoints:
            self.inject(side, "AGENTS.md", self.shared_agents)
        self.assert_equal()
        self.inject("local", "CONTEXT.md", self.shared_context + b"planned edit\n")
        path = self.state / "stale.json"
        plan = self.cli("plan", out=str(path))
        if not plan["ok"] or len(plan["plan"]["actions"]) != 1:
            raise AssertionError("stale setup did not produce one valid action")
        self.inject("local", "CONTEXT.md", self.shared_context + b"edit after plan\n")
        return self.rejected_apply(path, "stale or invalid plan")

    def recovery(self):
        self.inject("local", "CONTEXT.md", self.shared_context)
        self.assert_equal()
        return self.transfer("local", "AGENTS.md", self.shared_agents + b"recovery sync\n", "recovery")

    def run(self):
        try:
            # Confirm that the private report is writable before fixture mutations.
            self.save()
            self.step("preflight", self.preflight)
            self.step("baseline", lambda: self.cli("baseline"))
            self.shared_agents = self.original["local"]["AGENTS.md"] + b"acceptance local push\n"
            self.step("local_AGENTS_push", lambda: self.transfer(
                "local", "AGENTS.md", self.shared_agents, "local-push"))
            self.shared_context = self.original["remote"]["CONTEXT.md"] + b"acceptance remote pull\n"
            self.step("remote_CONTEXT_pull", lambda: self.transfer(
                "remote", "CONTEXT.md", self.shared_context, "remote-pull"))
            self.step("conflict_plan_and_apply_refused", self.conflict)
            self.step("stale_apply_refused", self.stale)
            self.step("restore_and_sync", self.recovery)
            self.report["ok"] = True
        except Exception as exc:
            self.report["failure"] = {"step": self.stage, "type": type(exc).__name__,
                                      "error": str(exc)}
            observations = {}
            for side, endpoint in self.endpoints.items():
                try:
                    snapshot = endpoint.call("inventory")
                    observations[side] = {key: snapshot[key] for key in ("head", "branch", "files")}
                except Exception as read_error:
                    observations[side] = {"read_error": str(read_error)}
            self.report["failure"]["content_hashes"] = observations
        self.save()
        return 0 if self.report["ok"] else 2


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="private absolute fixture config path")
    parser.add_argument("--report", required=True, help="fresh private absolute report path")
    args = parser.parse_args(argv)
    try:
        harness = Acceptance(args.config, args.report)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"ok": False, "failure": {"step": "configuration", "error": str(exc)}},
                         ensure_ascii=False))
        return 2
    try:
        code = harness.run()
    except OSError as exc:
        print(json.dumps({"ok": False, "failure": {"step": harness.stage,
                         "error": "cannot persist private report: " + str(exc)},
                         "completed_steps": [step["name"] for step in harness.report["steps"]]},
                         ensure_ascii=False))
        return 2
    print(json.dumps({"ok": harness.report["ok"], "report": str(harness.report_path),
                      "completed_steps": [step["name"] for step in harness.report["steps"]],
                      "failure": harness.report.get("failure")}, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
