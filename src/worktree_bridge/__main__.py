"""Conservative one-shot worktree synchronization CLI."""

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
from pathlib import PurePosixPath
import shlex
import subprocess
import sys
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

    def call(self, op, **kwargs):
        req = {"op": op, "root": self.root, **kwargs}
        if self.spec["kind"] == "local":
            return agent.dispatch(req)
        if self.spec["kind"] != "ssh":
            raise ValueError("remote kind must be local or ssh")
        host = self.spec["host"]
        port = self.spec.get("port", 22)
        if not isinstance(host, str) or not host or host.startswith("-") or any(c.isspace() for c in host):
            raise ValueError("invalid SSH host")
        if not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("invalid SSH port")
        source = Path(agent.__file__).read_bytes()
        encoded = base64.b64encode(zlib.compress(source)).decode("ascii")
        code = "import base64,zlib;exec(zlib.decompress(base64.b64decode('" + encoded + "')))"
        remote_command = "python3 -c " + shlex.quote(code)
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
               "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=10",
               "-o", "ServerAliveCountMax=1", "-p", str(port), host, remote_command]
        proc = subprocess.run(cmd, input=canonical(req).encode("utf-8"),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=45, check=False)
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
                     "port": other.get("port", 22), "root": str(PurePosixPath(other["root"]))}
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


def run(args):
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


def main():
    parser = argparse.ArgumentParser(prog="worktree-bridge")
    parser.add_argument("--config", required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sub.add_parser("baseline").add_argument("--refresh", action="store_true")
    sub.add_parser("plan").add_argument("--out")
    sub.add_parser("apply").add_argument("--plan", required=True)
    args = parser.parse_args()
    try:
        result = run(args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 2
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    sys.exit(main())
