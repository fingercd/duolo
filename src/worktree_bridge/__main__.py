"""Conservative one-shot worktree synchronization CLI."""

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
from pathlib import PurePosixPath
import re
import shlex
import subprocess
import sys
import time
import uuid
import zlib

from . import agent


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def checksum(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


class Endpoint:
    def __init__(self, spec):
        self.spec = spec
        self.root = spec["root"]
        if not Path(self.root).is_absolute():
            # SSH POSIX absolute paths are also recognized below.
            if not (spec["kind"] == "ssh" and self.root.startswith("/")):
                raise ValueError("endpoint root must be absolute")
        if spec["kind"] == "ssh":
            self.ssh_args, self.ssh_identity = self._ssh_configuration()
        elif spec["kind"] != "local":
            raise ValueError("remote kind must be local or ssh")

    def _ssh_configuration(self):
        host = self.spec["host"]
        port = self.spec.get("port")
        if not isinstance(host, str) or not host or host.startswith("-") or any(c.isspace() for c in host):
            raise ValueError("invalid SSH host")
        if port is not None and (type(port) is not int or not 1 <= port <= 65535):
            raise ValueError("invalid SSH port")
        args = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=10",
                "-o", "ServerAliveCountMax=1"]
        if port is not None:
            args.extend(["-p", str(port)])
        # -G evaluates the existing SSH configuration without connecting. Binding
        # its endpoint fields also detects a later alias/port change in a plan.
        proc = subprocess.run([*args, "-G", host], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=15, check=False,
                              creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        if proc.returncode:
            raise RuntimeError("cannot evaluate SSH configuration: "
                               + proc.stderr.decode("utf-8", "replace").strip())
        resolved = {}
        for line in proc.stdout.decode("utf-8", "replace").splitlines():
            fields = line.split(None, 1)
            if len(fields) == 2 and fields[0] in ("hostname", "user", "port"):
                resolved[fields[0]] = fields[1]
        if set(resolved) != {"hostname", "user", "port"}:
            raise ValueError("SSH configuration lacks hostname, user or port")
        resolved["port"] = int(resolved["port"])
        return [*args, host], resolved

    def call(self, op, **kwargs):
        req = {"op": op, "root": self.root, **kwargs}
        if self.spec["kind"] == "local":
            return agent.dispatch(req)
        source = Path(agent.__file__).read_bytes()
        encoded = base64.b64encode(zlib.compress(source)).decode("ascii")
        code = "import base64,zlib;exec(zlib.decompress(base64.b64decode('" + encoded + "')))"
        remote_command = "python3 -c " + shlex.quote(code)
        cmd = [*self.ssh_args, remote_command]
        proc = subprocess.run(cmd, input=canonical(req).encode("utf-8"),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=45, check=False,
                              creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        if proc.returncode:
            raise RuntimeError("SSH endpoint failed: " + proc.stderr.decode("utf-8", "replace").strip())
        try:
            response = json.loads(proc.stdout)
        except (ValueError, UnicodeError) as exc:
            raise RuntimeError("SSH endpoint returned invalid JSON") from exc
        if not response.get("ok"):
            raise RuntimeError("SSH endpoint: " + str(response.get("error")))
        return response["result"]


def configuration(path):
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    local_root = config["local_root"]
    remote = config["remote"]
    state_dir = Path(config["state_dir"])
    if not Path(local_root).is_absolute() or not state_dir.is_absolute():
        raise ValueError("local_root and state_dir must be absolute")
    local = Endpoint({"kind": "local", "root": local_root})
    other = Endpoint(remote)
    roots = [Path(local_root).resolve()]
    if remote["kind"] == "local":
        roots.append(Path(remote["root"]).resolve())
        if roots[0] == roots[1] or roots[0] in roots[1].parents or roots[1] in roots[0].parents:
            raise ValueError("local and remote roots must be separate worktrees")
    state = state_dir.resolve()
    if any(state == root or root in state.parents for root in roots):
        raise ValueError("state_dir must be outside both worktrees")
    return local, other, state


def endpoint_identity(local, remote):
    other = remote.spec
    if other["kind"] == "local":
        remote_id = {"kind": "local", "root": str(Path(other["root"]).resolve())}
    else:
        remote_id = {"kind": "ssh", "host": other["host"],
                     "resolved": remote.ssh_identity, "root": str(PurePosixPath(other["root"]))}
    return {"local": {"kind": "local", "root": str(Path(local.root).resolve())},
            "remote": remote_id}


def snapshots(local, remote):
    return {"local": local.call("inventory"), "remote": remote.call("inventory")}


def same_git(snap):
    a, b = snap["local"], snap["remote"]
    return bool(a["branch"]) and a["head"] == b["head"] and a["branch"] == b["branch"]


def baseline_path(state):
    return state / "baseline.json"


def load_baseline(state, identity):
    path = baseline_path(state)
    if not path.exists():
        raise ValueError("baseline missing; run baseline after reconciling both trees")
    base = json.loads(path.read_text(encoding="utf-8"))
    if base.get("schema") != 1 or checksum({k: v for k, v in base.items() if k != "id"}) != base.get("id"):
        raise ValueError("invalid baseline")
    if base.get("endpoints") != identity:
        raise ValueError("baseline belongs to different endpoints")
    return base


def make_baseline(snap, identity):
    base = {"schema": 1, "head": snap["local"]["head"],
            "branch": snap["local"]["branch"], "files": snap["local"]["files"],
            "endpoints": identity}
    base["id"] = checksum(base)
    return base


def make_plan(base, snap):
    a, b = snap["local"], snap["remote"]
    blockers = []
    if not same_git(snap):
        blockers.append({"reason": "git_mismatch", "local_head": a["head"],
                         "remote_head": b["head"], "local_branch": a["branch"],
                         "remote_branch": b["branch"]})
    if a["head"] != base["head"] or b["head"] != base["head"]:
        blockers.append({"reason": "baseline_head_changed", "baseline_head": base["head"]})
    if a["branch"] != base["branch"]:
        blockers.append({"reason": "baseline_branch_changed"})
    if a["unmerged"] or b["unmerged"]:
        blockers.append({"reason": "unmerged_index", "local": a["unmerged"],
                         "remote": b["unmerged"]})
    if a["git_operations"] or b["git_operations"]:
        blockers.append({"reason": "git_operation_in_progress", "local": a["git_operations"],
                         "remote": b["git_operations"]})
    folded = {}
    for path in sorted(set(base["files"]) | set(a["files"]) | set(b["files"])):
        key = path.casefold()
        if key in folded and folded[key] != path:
            blockers.append({"reason": "case_collision", "paths": [folded[key], path]})
        folded[key] = path
    actions = []
    paths = sorted(set(base["files"]) | set(a["files"]) | set(b["files"]))
    for path in paths:
        old = base["files"].get(path)
        left, right = a["files"].get(path), b["files"].get(path)
        if old is not None and (left is None or right is None):
            blockers.append({"reason": "deletion_or_exclusion", "path": path})
        elif left == right:
            continue
        elif left == old and right != old:
            actions.append({"path": path, "source": "remote", "dest": "local",
                            "source_hash": right, "dest_hash": left})
        elif right == old and left != old:
            actions.append({"path": path, "source": "local", "dest": "remote",
                            "source_hash": left, "dest_hash": right})
        else:
            blockers.append({"reason": "conflict", "path": path})
    plan = {"schema": 1, "baseline_id": base["id"], "endpoints": base["endpoints"],
            "snapshots": snap,
            "actions": actions, "blockers": blockers}
    plan["id"] = checksum(plan)
    return plan


def require_clean_plan(plan):
    if plan["blockers"]:
        raise ValueError("plan has blockers: " + canonical(plan["blockers"]))


def preflight_destinations(plan, endpoints):
    for action in plan["actions"]:
        if action["dest_hash"] is None:
            context = plan["snapshots"][action["dest"]]
            try:
                endpoints[action["dest"]].call("check_target", path=action["path"],
                                               expect_head=context["head"],
                                               expect_branch=context["branch"])
            except (OSError, ValueError, RuntimeError) as exc:
                plan["blockers"].append({"reason": "destination_unselected", "path": action["path"],
                                         "detail": str(exc)})
    plan["id"] = checksum({key: value for key, value in plan.items() if key != "id"})
    return plan


def execute(plan, state, endpoints):
    require_clean_plan(plan)
    journal_dir = state / "journal" / uuid.uuid4().hex
    journal_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    backups = []
    for index, action in enumerate(plan["actions"]):
        dest = endpoints[action["dest"]]
        if action["dest_hash"] is not None:
            context = plan["snapshots"][action["dest"]]
            old = dest.call("read", path=action["path"], expect_head=context["head"],
                            expect_branch=context["branch"])
            if old["sha256"] != action["dest_hash"]:
                raise ValueError("destination changed before backup: " + action["path"])
            backup_name = str(index) + ".bin"
            (journal_dir / backup_name).write_bytes(base64.b64decode(old["data"]))
        else:
            backup_name = None
        backups.append({"action": action, "backup": backup_name})
    journal = {"schema": 1, "plan_id": plan["id"], "status": "in_progress",
               "completed": [], "backups": backups}
    save_json(journal_dir / "journal.json", journal)
    try:
        for index, action in enumerate(plan["actions"]):
            src, dst = endpoints[action["source"]], endpoints[action["dest"]]
            source_git = plan["snapshots"][action["source"]]
            dest_git = plan["snapshots"][action["dest"]]
            payload = src.call("read", path=action["path"], expect_head=source_git["head"],
                               expect_branch=source_git["branch"])
            if payload["sha256"] != action["source_hash"]:
                raise ValueError("source changed: " + action["path"])
            result = dst.call("write", path=action["path"], data=payload["data"],
                              expected=action["dest_hash"], expect_head=dest_git["head"],
                              expect_branch=dest_git["branch"])
            if result["sha256"] != action["source_hash"]:
                raise ValueError("write verification failed: " + action["path"])
            journal["completed"].append(index)
            save_json(journal_dir / "journal.json", journal)
        post = {side: endpoint.call("inventory") for side, endpoint in endpoints.items()}
        expected = {side: dict(plan["snapshots"][side]["files"]) for side in endpoints}
        for action in plan["actions"]:
            expected[action["dest"]][action["path"]] = action["source_hash"]
        for side in endpoints:
            before, after = plan["snapshots"][side], post[side]
            if (after["head"] != before["head"] or after["branch"] != before["branch"]
                    or after["files"] != expected[side] or after["unmerged"]
                    or after["git_operations"]):
                raise ValueError("post-write inventory changed unexpectedly; baseline unchanged")
        if post["local"]["files"] != post["remote"]["files"]:
            raise ValueError("post-write inventories differ; baseline unchanged")
        new_base = make_baseline(post, plan["endpoints"])
        save_json(baseline_path(state), new_base)
        journal["status"] = "complete"
        save_json(journal_dir / "journal.json", journal)
        return {"applied": len(plan["actions"]), "baseline_id": new_base["id"],
                "journal": str(journal_dir / "journal.json")}
    except Exception:
        journal["status"] = "interrupted"
        save_json(journal_dir / "journal.json", journal)
        raise


def _run_once(args):
    local, remote, state = configuration(args.config)
    identity = endpoint_identity(local, remote)
    if args.command == "plan" and args.out:
        output = Path(args.out).resolve()
        roots = [Path(local.root).resolve()]
        if remote.spec["kind"] == "local":
            roots.append(Path(remote.root).resolve())
        if any(output == root or root in output.parents for root in roots):
            raise ValueError("plan output must be outside both visible worktrees")
        if output in (Path(args.config).resolve(), baseline_path(state).resolve()):
            raise ValueError("plan output cannot replace config or baseline")
    endpoints = {"local": local, "remote": remote}
    snap = snapshots(local, remote)
    if args.command == "status":
        left, right = snap["local"]["files"], snap["remote"]["files"]
        differences = {"only_local": sorted(set(left) - set(right)),
                       "only_remote": sorted(set(right) - set(left)),
                       "different": sorted(p for p in set(left) & set(right) if left[p] != right[p])}
        exists = baseline_path(state).exists()
        try:
            if exists:
                load_baseline(state, identity)
            baseline_matches = exists
        except (OSError, ValueError, KeyError):
            baseline_matches = False
        return {"ok": True, "git_match": same_git(snap), "baseline_exists": exists,
                "baseline_matches_endpoints": baseline_matches,
                "differences": differences,
                "endpoints": {side: {key: value for key, value in item.items() if key != "files"}
                              | {"file_count": len(item["files"])} for side, item in snap.items()}}
    if args.command == "baseline":
        if baseline_path(state).exists() and not args.refresh:
            raise ValueError("baseline already exists")
        if args.refresh and not baseline_path(state).exists():
            raise ValueError("baseline missing; nothing to refresh")
        if not same_git(snap):
            raise ValueError("initial git branch/HEAD differs")
        if snap["local"]["unmerged"] or snap["remote"]["unmerged"]:
            raise ValueError("unmerged index must be resolved before baseline")
        if snap["local"]["git_operations"] or snap["remote"]["git_operations"]:
            raise ValueError("Git operation in progress must finish before baseline")
        if any(reason == "missing_worktree_file" for side in snap.values()
               for reason in side["excluded"].values()):
            raise ValueError("tracked worktree deletion must be resolved before baseline")
        if snap["local"]["files"] != snap["remote"]["files"]:
            raise ValueError("initial divergence: file content or selection differs")
        folded = {}
        for path in set(snap["local"]["files"]) | set(snap["remote"]["files"]):
            key = path.casefold()
            if key in folded and folded[key] != path:
                raise ValueError("case-insensitive path collision: " + folded[key] + " / " + path)
            folded[key] = path
        base = make_baseline(snap, identity)
        state.mkdir(mode=0o700, parents=True, exist_ok=True)
        old_backup = None
        if args.refresh:
            old = load_baseline(state, identity)
            removed = sorted(set(old["files"]) - set(base["files"]))
            if removed:
                raise ValueError("baseline refresh cannot discard selected files: " + canonical(removed))
            history = state / "baseline-history"
            history.mkdir(mode=0o700, parents=True, exist_ok=True)
            old_backup = history / (uuid.uuid4().hex + ".json")
            save_json(old_backup, old)
        save_json(baseline_path(state), base)
        return {"ok": True, "baseline_id": base["id"], "files": len(base["files"]),
                "previous_baseline": str(old_backup) if old_backup else None}
    base = load_baseline(state, identity)
    plan = preflight_destinations(make_plan(base, snap), endpoints)
    if args.command == "plan":
        if args.out:
            save_json(Path(args.out), plan)
        return {"ok": not bool(plan["blockers"]), "plan": plan, "plan_file": args.out}
    if args.command == "apply":
        supplied = json.loads(Path(args.plan).read_text(encoding="utf-8"))
        if supplied != plan:
            raise ValueError("stale or invalid plan; run plan again")
        if not same_git(snap):
            raise ValueError("git branch/HEAD differs")
        return {"ok": True, **execute(plan, state, endpoints)}
    raise ValueError("unknown command")


def _start_service(args):
    from . import client
    try:
        current = client.get_status(args.config)
        return {"ok": True, "already_running": True, "url": client.service_info(args.config)["url"],
                "state": current["state"]}
    except client.ServiceUnavailable:
        pass
    _, state, fingerprint = client.configuration_info(args.config)
    state.mkdir(parents=True, exist_ok=True)
    executable = Path(sys.executable)
    if os.name == "nt" and executable.with_name("pythonw.exe").is_file():
        # The Windows venv console launcher can allocate a new console for its
        # child even when its own startup window is hidden. Use the GUI launcher.
        executable = executable.with_name("pythonw.exe")
    command = [str(executable), "-u", "-m", "worktree_bridge", "--config",
               str(Path(args.config).resolve()), "serve"]
    if args.port is not None:
        command.extend(["--port", str(args.port)])
    environment = dict(os.environ)
    package_root = str(Path(__file__).resolve().parents[1])
    environment["PYTHONPATH"] = package_root + (os.pathsep + environment["PYTHONPATH"]
                                               if environment.get("PYTHONPATH") else "")
    environment["PYTHONUTF8"] = "1"
    startup_id = uuid.uuid4().hex
    environment["WTB_STARTUP_ID"] = startup_id
    options = {"stdin": subprocess.DEVNULL, "env": environment, "close_fds": True}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 0
        options["startupinfo"] = startup
    else:
        options["start_new_session"] = True
    log_path = state / "service.log"
    with log_path.open("ab") as log:
        process = subprocess.Popen(command, stdout=log, stderr=log, **options)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("Service could not start; inspect " + str(log_path))
        try:
            info = client.service_info(args.config)
            current = client.get_status(args.config)
            # Windows venv launchers may host Python in a child with another PID.
            if info.get("startup_id") == startup_id and info["config_fingerprint"] == fingerprint:
                return {"ok": True, "pid": info["pid"], "url": info["url"],
                        "state": current["state"], "log": str(log_path)}
        except client.ServiceUnavailable:
            pass
        time.sleep(.1)
    raise RuntimeError("Service startup is still unconfirmed; inspect the existing process log before retrying: "
                       + str(log_path))


def _service_command(args):
    from . import client
    if args.command == "start":
        return _start_service(args)
    if args.command == "serve":
        from .web import run_server
        run_server(args.config, port=args.port or 0)
        return {"ok": True, "stopped": True}
    if args.command == "status":
        return {"ok": True, **client.get_status(args.config)}
    if args.command == "conflicts":
        status = client.get_status(args.config)
        return {"ok": True, "revision": status["revision"], "conflicts": status["conflicts"]}
    if args.command == "wait":
        return {"ok": True, **client.wait_until_synced(args.config, args.timeout)}
    params = {}
    if args.command == "resolve":
        params = {"path": args.path, "choice": args.take, "expected_revision": args.revision}
    elif args.command == "checkpoint":
        params = {"message": args.message}
        if args.tag:
            params["tag"] = args.tag
    result = client.action(args.config, args.command, params)
    if args.command == "sync" and args.wait:
        return {"ok": True, **client.wait_until_synced(args.config, args.timeout)}
    return result


def run(args):
    if args.command == "init":
        from .registry import init_project
        if args.config:
            raise ValueError("Use init --from-config to import an existing pairing")
        remote = None
        if args.remote:
            if not args.path:
                raise ValueError("init --remote requires --path /absolute/remote/repository")
            remote = {"kind": "ssh", "host": args.remote, "root": args.path}
            if args.port is not None:
                remote["port"] = args.port
        elif args.path is not None or args.port is not None:
            raise ValueError("--path and --port require --remote")
        if args.local_peer:
            remote = {"kind": "local", "root": str(Path(args.local_peer).resolve())}
        return init_project(Path.cwd(), remote=remote, name=args.name, from_config=args.from_config)
    if args.command == "projects":
        from .registry import list_projects
        return {"ok": True, "projects": list_projects()}
    if args.command == "status" and getattr(args, "fresh", False):
        return _run_once(args)
    if args.command in {"start", "serve", "stop", "status", "sync", "pause",
                        "resume", "conflicts", "resolve", "wait", "checkpoint"}:
        return _service_command(args)
    # One-shot commands and the daemon must never write a shared pair together.
    from .client import configuration_info
    from .service import ControllerLock
    config, state, _ = configuration_info(args.config)
    state_lock = ControllerLock(state)
    worktree_lock = None
    remote_lease = None
    state_lock.acquire()
    try:
        root = agent.root_path(config["local_root"])
        git_dir = Path(agent.git(root, "rev-parse", "--absolute-git-dir").decode("utf-8").strip())
        worktree_lock = ControllerLock(git_dir / "worktree-bridge-controller")
        worktree_lock.acquire()
        if args.command == "apply":
            from .transport import Peer
            remote_lease = Peer(config["remote"])
            remote_lease.call("claim_controller", instance_id="one-shot-" + uuid.uuid4().hex)
        return _run_once(args)
    finally:
        if remote_lease is not None:
            remote_lease.close()
        if worktree_lock is not None:
            worktree_lock.release()
        state_lock.release()


def _plain(value):
    return re.sub(r"[\x00-\x1f\x7f]", " ", str(value))


def format_status(status):
    """A compact terminal view of observations, never an implied fresh scan."""
    state = status.get("state", "fresh_snapshot")
    lines = [_plain(status.get("name", "Worktree Bridge")) + "  [" + state.upper() + "]"]
    endpoints = status.get("git", status.get("endpoints", {}))
    for side in ("local", "remote"):
        current = endpoints.get(side, {})
        dirty = "unknown" if current.get("dirty") is None else ("modified" if current["dirty"] else "clean")
        lines.append(f"  {side:6} {_plain(current.get('branch') or '-')}  "
                     f"{_plain(current.get('head') or '-')[:12]}  {dirty}")
    files = status.get("files", {})
    if files:
        lines.append(f"  pending {files.get('pending_count', 0)}  conflicts {files.get('conflict_count', 0)}")
    for change in status.get("pending", [])[:20]:
        lines.append("  " + _plain(change.get("direction", "changed")) + "  " + _plain(change["path"]))
    for conflict in status.get("conflicts", [])[:20]:
        lines.append("  CONFLICT " + _plain(conflict["path"]) + "  " + _plain(conflict.get("reason", "")))
    if status.get("last_error"):
        error = status["last_error"]
        lines.append("  " + _plain(error.get("message", error) if isinstance(error, dict) else error))
    observed = status.get("observed_at")
    if observed:
        lines.append("  observed " + time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(observed)))
    if "revision" in status:
        lines.append("  revision " + _plain(status["revision"]))
    return "\n".join(lines)


def watch_status(config_path, interval=.5):
    from . import client
    if not .1 <= interval <= 60:
        raise ValueError("watch interval must be between 0.1 and 60 seconds")
    # Fail promptly on a missing initial registration/service; never auto-start.
    current = client.get_status(config_path)
    previous = None
    last_event = 0
    while True:
        key = canonical({k: current.get(k) for k in ("instance_id", "state", "revision", "last_error")})
        if key != previous:
            print(time.strftime("%H:%M:%S") + " " + format_status(current), flush=True)
            previous = key
        for event in current.get("events", []):
            if event["time"] > last_event:
                print("  " + _plain(event["level"]).upper() + " " + _plain(event["message"]), flush=True)
                for path in event.get("paths", []):
                    print("    " + _plain(path), flush=True)
                last_event = max(last_event, event["time"])
        time.sleep(interval)
        try:
            current = client.get_status(config_path)
        except client.ServiceUnavailable as exc:
            current = {"state": "offline", "last_error": str(exc)}


def main():
    parser = argparse.ArgumentParser(prog="worktree-bridge")
    parser.add_argument("--version", action="version", version="%(prog)s 0.3.0")
    parser.add_argument("--config", help="advanced explicit configuration override")
    sub = parser.add_subparsers(dest="command", required=True)
    initialize = sub.add_parser("init", help="register the current existing Git worktree")
    binding = initialize.add_mutually_exclusive_group()
    binding.add_argument("--remote", help="SSH host or existing SSH alias")
    binding.add_argument("--local-peer", help="second local checkout, useful for testing")
    binding.add_argument("--from-config", help="import an existing private pairing configuration")
    initialize.add_argument("--path", help="absolute path of the remote Git checkout")
    initialize.add_argument("--port", type=int)
    initialize.add_argument("--name")
    sub.add_parser("projects", help="list registered local worktrees without network access")
    status = sub.add_parser("status", help="read instant cached service status")
    status.add_argument("--fresh", action="store_true", help="explicit full scan without using the cache")
    status.add_argument("--short", action="store_true", help="compact human-readable terminal output")
    sub.add_parser("watch", help="print state changes and conflicts until Ctrl+C").add_argument("--interval", type=float, default=.5)
    sub.add_parser("baseline").add_argument("--refresh", action="store_true")
    sub.add_parser("plan").add_argument("--out")
    sub.add_parser("apply").add_argument("--plan", required=True)
    sub.add_parser("start", help="start a hidden background service").add_argument("--port", type=int)
    sub.add_parser("serve", help="run the service in the foreground").add_argument("--port", type=int)
    sub.add_parser("stop")
    sync = sub.add_parser("sync")
    sync.add_argument("--wait", action="store_true")
    sync.add_argument("--timeout", type=float, default=30)
    sub.add_parser("pause")
    sub.add_parser("resume")
    sub.add_parser("conflicts")
    resolve = sub.add_parser("resolve")
    resolve.add_argument("path")
    resolve.add_argument("--take", choices=("local", "remote"), required=True)
    resolve.add_argument("--revision", required=True)
    sub.add_parser("wait").add_argument("--timeout", type=float, default=30)
    checkpoint = sub.add_parser("checkpoint")
    checkpoint.add_argument("-m", "--message", required=True)
    checkpoint.add_argument("--tag")
    sub.add_parser("mcp", help="serve the optional MCP stdio adapter")
    args = parser.parse_args()
    try:
        if args.command not in ("init", "projects") and args.config is None:
            from .registry import find_config
            args.config = str(find_config(Path.cwd()))
        if args.command == "mcp":
            from .mcp_server import run as run_mcp
            run_mcp(args.config)
            return 0
        if args.command == "watch":
            watch_status(args.config, args.interval)
            return 0
        result = run(args)
        if args.command == "status" and args.short:
            print(format_status(result))
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 2
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.TimeoutExpired) as exc:
        if args.command == "mcp":
            print(str(exc), file=sys.stderr)
            return 2
        error = {"ok": False, "error": str(exc)}
        if hasattr(exc, "last_status"):
            error.update(timed_out=True, last_status=exc.last_status)
        print(json.dumps(error, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    sys.exit(main())
