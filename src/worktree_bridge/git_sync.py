"""Controller-side Git coordination over the fixed-root peer protocol."""

import copy
import json
import os
from pathlib import Path
import tempfile
import time
import uuid


class GitCoordinator:
    def __init__(self, peers, state_dir, identity=None):
        self.peers = peers
        self.state_dir = Path(state_dir)
        self.identity = identity

    def _snapshots(self):
        return {side: peer.call("snapshot", force=True) for side, peer in self.peers.items()}

    def _record(self, data, path=None):
        directory = self.state_dir / "git-journals"
        directory.mkdir(parents=True, exist_ok=True)
        path = path or directory / (uuid.uuid4().hex + ".json")
        data["updated_at"] = time.time()
        fd, temporary = tempfile.mkstemp(prefix=".journal-", dir=str(directory))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, str(path))
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return path

    @staticmethod
    def _result(snapshots, baseline, blocked=False, reason=None, **extra):
        return {"snapshots": snapshots, "baseline": baseline, "blocked": blocked,
                "reason": reason, "followed": False, **extra}

    def _safe_status(self, snapshots):
        for side, peer in self.peers.items():
            state = peer.call("git_status")
            if (state["head"], state["branch"]) != (snapshots[side]["head"], snapshots[side]["branch"]):
                return "Git changed during coordination"
            if state["staged"] or state["unmerged"] or state["git_operations"] or state.get("index_flags"):
                return side + ": staged work, index flags or Git operation blocks Git coordination"
            excluded = snapshots[side].get("excluded", {})
            for path in state.get("dirty_paths", []):
                if path in excluded and excluded[path] not in ("excluded_by_policy", "missing_worktree_file"):
                    return side + ": unsupported dirty path blocks Git coordination: " + path
        return None

    def reconcile(self, snapshots, baseline):
        if baseline is None:
            return self._result(snapshots, baseline, True, "Git following requires a common baseline")
        if self.identity is not None and baseline.get("endpoints") != self.identity:
            return self._result(snapshots, baseline, True, "baseline belongs to different endpoints")
        branches = {item["branch"] for item in snapshots.values()}
        if len(branches) != 1 or not next(iter(branches)) or baseline["branch"] not in branches:
            return self._result(snapshots, baseline, True, "branch differs from common baseline")
        heads = {side: item["head"] for side, item in snapshots.items()}
        if all(head == baseline["head"] for head in heads.values()):
            return self._result(snapshots, baseline)
        reason = self._safe_status(snapshots)
        if reason:
            return self._result(snapshots, baseline, True, reason)
        advanced = [side for side, head in heads.items() if head != baseline["head"]]
        if len(advanced) == 2:
            if heads["local"] != heads["remote"]:
                return self._result(snapshots, baseline, True, "both endpoints advanced: divergent histories require review")
            if not self.peers["local"].call("git_is_descendant", base_head=baseline["head"], incoming_head=heads["local"])["descendant"]:
                return self._result(snapshots, baseline, True, "common HEAD is not a baseline descendant")
            updated = copy.deepcopy(baseline)
            updated["head"] = heads["local"]
            return self._result(snapshots, updated)
        source = advanced[0]
        target = "remote" if source == "local" else "local"
        return self._follow(source, target, snapshots, baseline)

    def _follow(self, source, target, snapshots, baseline, tag=None):
        data = {"schema": 1, "operation": "follow", "status": "running", "phase": "prepared",
                "source": source, "target": target, "before_head": baseline["head"],
                "incoming_head": snapshots[source]["head"], "branch": baseline["branch"],
                "before_files": snapshots[target]["files"], "tag": tag}
        path = self._record(data)
        try:
            src_git = {key: snapshots[source][key] for key in ("head", "branch")}
            dst_git = {key: snapshots[target][key] for key in ("head", "branch")}
            bundle = self.peers[source].call("git_bundle_create", expected_git=src_git,
                                              base_head=baseline["head"])
            data["phase"] = "bundle_created"
            self._record(data, path)
            self.peers[target].call("git_bundle_import", data=bundle["data"], expected_git=dst_git,
                                      base_head=baseline["head"], incoming_head=src_git["head"])
            data["phase"] = "objects_imported"
            self._record(data, path)
            result = self.peers[target].call("git_follow", expected_git=dst_git,
                                              base_head=baseline["head"], incoming_head=src_git["head"],
                                              baseline_files=baseline["files"],
                                              expected_files=snapshots[target]["files"], tag=tag)
            data.update(phase="peer_followed", peer_journal=result["journal"])
            self._record(data, path)
            post = self._snapshots()
            if any((item["head"], item["branch"]) != (src_git["head"], src_git["branch"]) for item in post.values()):
                raise ValueError("Git changed before follow verification")
            updated = copy.deepcopy(baseline)
            updated.update(head=src_git["head"], branch=src_git["branch"])
            data.update(status="complete", phase="verified", after_files=post[target]["files"])
            self._record(data, path)
            return self._result(post, updated, followed=True, journal=str(path))
        except Exception as exc:
            data.update(status="failed", error=str(exc))
            self._record(data, path)
            try:
                snapshots = self._snapshots()
                data["observed_heads"] = {side: item["head"] for side, item in snapshots.items()}
            except Exception as observation:
                data["observation_error"] = str(observation)
            self._record(data, path)
            return self._result(snapshots, baseline, True, str(exc), journal=str(path))

    def checkpoint(self, message, tag=None, snapshots=None, baseline=None):
        snapshots = snapshots or self._snapshots()
        if baseline is None:
            raise ValueError("checkpoint requires a common baseline")
        if self.identity is not None and baseline.get("endpoints") != self.identity:
            raise ValueError("baseline belongs to different endpoints")
        local, remote = snapshots["local"], snapshots["remote"]
        if (local["head"], local["branch"]) != (remote["head"], remote["branch"]):
            raise ValueError("checkpoint requires matching Git HEAD and branch")
        if (local["head"], local["branch"]) != (baseline["head"], baseline["branch"]):
            raise ValueError("checkpoint Git differs from the common baseline")
        if local["files"] != remote["files"]:
            raise ValueError("checkpoint requires synchronized file contents")
        reason = self._safe_status(snapshots)
        if reason:
            raise ValueError(reason)
        if tag is not None:
            for peer in self.peers.values():
                if peer.call("git_tag_check", tag=tag)["exists"]:
                    raise ValueError("tag already exists")
        data = {"schema": 1, "operation": "checkpoint", "status": "running", "phase": "prepared",
                "before_head": local["head"], "tag": tag, "message": message}
        path = self._record(data)
        try:
            result = self.peers["local"].call("git_checkpoint",
                expected_git={key: local[key] for key in ("head", "branch")},
                expected_files=local["files"], message=message, tag=tag)
            data.update(phase="source_committed", incoming_head=result["head"], peer_journal=result["journal"])
            self._record(data, path)
            current = self._snapshots()
            followed = self._follow("local", "remote", current, baseline, tag=tag)
            if followed["blocked"]:
                raise ValueError("checkpoint exists on local endpoint; peer follow failed: " + followed["reason"])
            data.update(status="complete", phase="verified", follow_journal=followed["journal"])
            self._record(data, path)
            followed["checkpoint"] = {"head": result["head"], "tag": tag, "source": "local"}
            followed["journal"] = str(path)
            return followed
        except Exception as exc:
            data.update(status="failed", error=str(exc))
            self._record(data, path)
            raise ValueError(str(exc) + "; journal: " + str(path)) from exc
