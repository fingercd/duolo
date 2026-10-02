"""One serialized controller with a cheap, independently readable status cache."""

import base64
import copy
from concurrent.futures import ThreadPoolExecutor, wait
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import threading
import time
import uuid

from . import agent


def _checksum(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode()).hexdigest()


def _save(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class ControllerLock:
    """The OS lock, not an old PID file, establishes exclusive ownership."""

    def __init__(self, state):
        self.state = state
        self.handle = None

    def acquire(self):
        self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        handle = (self.state / "controller.lock").open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                if handle.seek(0, 2) == 0:
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise RuntimeError("another controller owns this state directory") from exc
        self.handle = handle

    def release(self):
        if self.handle is not None:
            self.handle.close()
            self.handle = None


class BridgeService:
    def __init__(self, config_path, *, peer_factory=None):
        self.config_path = Path(config_path).resolve()
        self.config = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.state_dir = Path(self.config["state_dir"])
        if not self.state_dir.is_absolute():
            raise ValueError("state_dir must be absolute")
        self.options = {"poll_interval": .25, "reconcile_interval": 30,
                        "auto_sync": True, "auto_git": True, "allow_delete": False,
                        "stale_after_seconds": 10,
                        "port": 0,
                        **self.config.get("service", {})}
        for field in ("poll_interval", "reconcile_interval", "stale_after_seconds"):
            value = self.options[field]
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError(field + " must be a finite positive number")
        if self.options["stale_after_seconds"] < 1:
            raise ValueError("stale_after_seconds must be at least 1")
        port = self.options["port"]
        if type(port) is not int or not 0 <= port <= 65535:
            raise ValueError("port must be an integer between 0 and 65535")
        for field in ("auto_sync", "auto_git", "allow_delete"):
            if type(self.options[field]) is not bool:
                raise ValueError(field + " must be a boolean")
        self.instance_id = uuid.uuid4().hex
        self.config_fingerprint = _checksum(self.config)
        self._peer_factory = peer_factory
        self._lock = threading.Lock()
        self._controller = ControllerLock(self.state_dir)
        self._worktree_lock = None
        self._queue = queue.Queue(maxsize=32)
        self._stop = threading.Event()
        self._thread = None
        self._snapshot_workers = ThreadPoolExecutor(max_workers=2, thread_name_prefix="worktree-bridge-snapshot")
        self._peers = {}
        self._snapshots = {}
        self._base = None
        self._identity = None
        self._unconfirmed = []
        self._paused = False
        self._initialization_error = None
        self._retry_at = {}
        self._failures = {}
        self._force_at = 0
        self._revision = 0
        self._view = {"schema": 1, "version": "0.4.0",
                      "name": self.config.get("name", Path(self.config["local_root"]).name),
                      "instance_id": self.instance_id,
                      "config_fingerprint": self.config_fingerprint,
                      "state": "initializing", "revision": "0", "updated_at": time.time(),
                      "observed_at": None,
                      "stale_observation": False,
                      "last_sync_at": None, "last_error": None, "paused": False,
                      "connection": {side: {"connected": False, "last_seen": None}
                                     for side in ("local", "remote")},
                      "files": {"local_count": 0, "remote_count": 0,
                                "pending_count": 0, "conflict_count": 0},
                      "git": {"local": {"head": None, "branch": None, "dirty": None},
                              "remote": {"head": None, "branch": None, "dirty": None},
                              "match": False, "auto_follow": self.options["auto_git"]},
                      "pending": [], "conflicts": [], "events": [],
                      "documents": {"local": {}, "remote": {}},
                      "recent_actions": [],
                      "performance": {"last_cycle_ms": None},
                      "capabilities": {**{k: True for k in ("sync", "observe", "pause", "resume", "resolve", "checkpoint", "stop")},
                                       **{k: self.options[k] for k in ("auto_sync", "auto_git", "allow_delete")}}}

    def view(self):
        with self._lock:
            result = copy.deepcopy(self._view)
        observed = result["observed_at"]
        if result["state"] == "synced" and (observed is None or time.time() - observed > self.options["stale_after_seconds"]):
            result["state"] = "checking"
            result["stale_observation"] = True
            result["last_error"] = {"code": "stale_observation", "message": "上次观察已过期，正在重新确认"}
        return result

    def _publish(self, **updates):
        with self._lock:
            before = _checksum({k: self._view[k] for k in ("state", "paused", "git", "files", "pending", "conflicts", "documents")})
            self._view.update(updates)
            after = _checksum({k: self._view[k] for k in ("state", "paused", "git", "files", "pending", "conflicts", "documents")})
            if before != after:
                self._revision += 1
            self._view["revision"] = str(self._revision)
            self._view["updated_at"] = time.time()

    def _event(self, level, message, **details):
        with self._lock:
            self._view["events"] = (self._view["events"] + [{"time": time.time(), "level": level,
                                                          "message": str(message), **details}])[-60:]

    def _connection(self, side, connected, last_seen=None):
        # Each snapshot worker updates only its own endpoint under this lock.
        # Copying the whole connection dictionary in two workers loses updates.
        with self._lock:
            connection = self._view["connection"][side]
            connection["connected"] = connected
            if last_seen is not None:
                connection["last_seen"] = last_seen
            self._view["updated_at"] = time.time()

    def action(self, name, params=None):
        params = {} if params is None else params
        try:
            if name not in ("sync", "observe", "pause", "resume", "resolve", "checkpoint", "stop"):
                raise ValueError("unknown action")
            if not isinstance(params, dict):
                raise ValueError("action parameters must be an object")
            allowed = {"resolve": {"path", "choice", "expected_revision"},
                       "checkpoint": {"message", "tag"}}.get(name, set())
            if set(params) - allowed:
                raise ValueError("unknown action parameters")
            if name == "resolve":
                agent.path_parts(params.get("path"))
                if params.get("choice") not in ("local", "remote"):
                    raise ValueError("choice must be local or remote")
                if params.get("expected_revision") != self.view()["revision"]:
                    return {"ok": False, "error": {"code": "stale_revision", "message": "status changed; refresh before resolving"}}
            if name == "checkpoint":
                if not isinstance(params.get("message"), str) or not params["message"].strip():
                    raise ValueError("checkpoint requires a commit message")
                if "tag" in params and not isinstance(params["tag"], str):
                    raise ValueError("tag must be a string")
            if self._stop.is_set() and name != "stop":
                raise ValueError("service is stopping")
            if self._initialization_error is not None and name != "stop":
                raise ValueError("service initialization failed: " + self._initialization_error)
            identifier = uuid.uuid4().hex
            if name == "stop":
                self.stop()
                self._action_status(identifier, name, "complete")
            else:
                with self._lock:
                    self._queue.put_nowait((identifier, name, dict(params)))
                    self._view["recent_actions"] = (self._view["recent_actions"] + [{"id": identifier, "name": name,
                                                                                  "status": "queued", "completed_at": None}])[-64:]
            return {"ok": True, "queued": True, "action": name, "id": identifier}
        except (ValueError, queue.Full) as exc:
            return {"ok": False, "error": {"code": "invalid_action", "message": str(exc) or "action queue is full"}}

    def _action_status(self, identifier, name, status, error=None):
        with self._lock:
            records = self._view["recent_actions"]
            record = next((r for r in records if r["id"] == identifier), None)
            if record is None:
                record = {"id": identifier, "name": name}
                records.append(record)
            record.update(status=status, completed_at=time.time() if status in ("complete", "failed") else None)
            if error is not None:
                record["error"] = error
            self._view["recent_actions"] = records[-64:]

    def start(self):
        if self._thread is not None:
            raise RuntimeError("service already started")
        self._controller.acquire()
        self._thread = threading.Thread(target=self._loop, name="worktree-bridge-controller", daemon=True)
        self._thread.start()
        return self

    def run(self):
        self._controller.acquire()
        self._loop()

    def stop(self):
        self._stop.set()
        # Interrupt only child peers owned by this instance; never signal a PID
        # obtained from stale metadata. Keep the action request responsive.
        def close_owned():
            for peer in list(self._peers.values()):
                peer.close()
        threading.Thread(target=close_owned, daemon=True).start()

    def join(self, timeout=None):
        if self._thread is not None:
            self._thread.join(timeout)

    def _initialize(self):
        # Existing endpoint identity remains authoritative for old baselines.
        from .__main__ import configuration, endpoint_identity, load_baseline
        local, remote, state = configuration(self.config_path)
        if self._worktree_lock is None:
            git_dir = Path(agent.git(Path(local.root), "rev-parse", "--absolute-git-dir").decode("utf-8").strip())
            self._worktree_lock = ControllerLock(git_dir / "worktree-bridge-controller")
            self._worktree_lock.acquire()
        self._identity = endpoint_identity(local, remote)
        self._specs = {"local": local.spec, "remote": remote.spec}
        if self._peer_factory is None:
            from .transport import Peer
            self._peer_factory = Peer
        if (state / "baseline.json").exists():
            self._base = load_baseline(state, self._identity)
        self._unconfirmed = []
        for path in (state / "journal").glob("*/journal.json"):
            record = json.loads(path.read_text(encoding="utf-8"))
            if (record.get("schema") == 1 and record.get("status") in ("writing", "interrupted")
                    and record.get("endpoints") == self._identity and record.get("expected_git")):
                self._unconfirmed.append((path, record))
        from .git_sync import GitCoordinator
        self._coordinator_type = GitCoordinator

    def _snapshot(self, side, force, barrier=False):
        now = time.time()
        if now < self._retry_at.get(side, 0):
            return False
        try:
            if side not in self._peers:
                peer = self._peer_factory(self._specs[side])
                self._peers[side] = peer
                if (self._specs[side].get("kind") == "ssh"
                        and peer.ssh_identity != self._identity[side]["resolved"]):
                    raise ValueError("SSH endpoint identity changed; baseline revalidation is required")
                if side == "remote":
                    peer.call("claim_controller", instance_id=self.instance_id)
            arguments = {"force": force}
            if barrier:
                arguments["barrier"] = True
            observed = time.time()
            snap = self._peers[side].call("snapshot", **arguments)
            received = time.time()
            # Endpoint clocks are diagnostic data, never a freshness clock.
            # Start time is conservative when an observation RPC takes long.
            snap["controller_observed_at"] = observed
            self._snapshots[side] = snap
            self._failures[side] = 0
            self._connection(side, True, received)
            return True
        except Exception as exc:
            peer = self._peers.pop(side, None)
            if peer is not None:
                peer.close()
            self._failures[side] = self._failures.get(side, 0) + 1
            self._retry_at[side] = time.time() + min(30, .5 * 2 ** min(self._failures[side] - 1, 6))
            self._connection(side, False)
            self._publish(state="offline",
                          last_error={"code": "offline", "message": side + ": " + str(exc)})
            self._event("error", side + ": " + str(exc))
            return False

    def _collect_snapshots(self, force, barrier=False):
        futures = {side: self._snapshot_workers.submit(self._snapshot, side, force, barrier)
                   for side in ("local", "remote")}
        wait(futures.values())
        return all([future.result() for future in futures.values()])

    def _publish_observation(self):
        # GitCoordinator may return snapshots obtained outside this controller's
        # timed RPC wrapper. Preserve the previous confirmed time until a timed
        # two-endpoint collection completes instead of fabricating a fresh time.
        if all("controller_observed_at" in s for s in self._snapshots.values()):
            self._publish(observed_at=min(s["controller_observed_at"] for s in self._snapshots.values()))

    def _save_base(self):
        self._base["id"] = _checksum({k: v for k, v in self._base.items() if k != "id"})
        _save(self.state_dir / "baseline.json", self._base)

    def _plan(self):
        left, right = self._snapshots["local"]["files"], self._snapshots["remote"]["files"]
        pending, conflicts = [], []
        old = self._base["files"]
        folded = {}
        for path in sorted(set(left) | set(right) | set(old)):
            key = path.casefold()
            if key in folded and folded[key] != path:
                conflicts.append({"path": path, "reason": "case_collision", "local_hash": left.get(path), "remote_hash": right.get(path)})
            folded[key] = path
            a, b, previous = left.get(path), right.get(path), old.get(path)
            exclusions = [self._snapshots[s].get("excluded", {}).get(path) for s in ("local", "remote")]
            if any(x is not None and x != "missing_worktree_file" for x in exclusions):
                reason = "selection_changed"
            elif a == b:
                continue
            elif a != previous and b != previous:
                reason = "both_changed"
            elif previous is not None and (a is None or b is None) and not self.options["allow_delete"]:
                reason = "delete_requires_allow_delete"
            else:
                source = "local" if b == previous else "remote"
                pending.append({"path": path, "direction": source + "_to_" + ("remote" if source == "local" else "local"),
                                "kind": "delete" if (a if source == "local" else b) is None else ("create" if previous is None else "modify")})
                continue
            conflicts.append({"path": path, "reason": reason, "local_hash": a, "remote_hash": b})
        return pending, conflicts

    def _git_context(self, side):
        return {k: self._snapshots[side][k] for k in ("head", "branch")}

    def _advance_equal(self):
        changed = False
        a, b = self._snapshots["local"]["files"], self._snapshots["remote"]["files"]
        for path in set(a) | set(b) | set(self._base["files"]):
            if a.get(path) == b.get(path) and self._base["files"].get(path) != a.get(path):
                if a.get(path) is None:
                    self._base["files"].pop(path, None)
                else:
                    self._base["files"][path] = a[path]
                changed = True
        if changed:
            self._save_base()

    def _confirm_interrupted(self):
        """Observe prior intents after reconnect; never send their writes again."""
        if self._base is None:
            return
        for path, journal in self._unconfirmed:
            changed = False
            completed = set(journal["completed"])
            for item in journal["actions"]:
                dest = item["dest"]
                if item["path"] in completed or self._git_context(dest) != journal["expected_git"][dest]:
                    continue
                if self._base["files"].get(item["path"]) not in (item["dest_hash"], item["source_hash"]):
                    # A later verified sync superseded this intent. An old
                    # journal must never roll a newer per-file baseline back.
                    continue
                if self._snapshots[dest]["files"].get(item["path"]) == item["source_hash"]:
                    if item["source_hash"] is None:
                        self._base["files"].pop(item["path"], None)
                    else:
                        self._base["files"][item["path"]] = item["source_hash"]
                    completed.add(item["path"])
                    changed = True
            if changed:
                self._save_base()
            status = "complete" if len(completed) == len(journal["actions"]) else "interrupted"
            if changed or status != journal["status"]:
                journal["completed"] = sorted(completed)
                journal["status"] = status
                _save(path, journal)
        self._unconfirmed = [(p, j) for p, j in self._unconfirmed if j["status"] != "complete"]

    def _apply(self, pending):
        actions = []
        for item in pending:
            source, dest = item["direction"].split("_to_")
            path = item["path"]
            actions.append({"path": path, "source": source, "dest": dest,
                            "source_hash": self._snapshots[source]["files"].get(path),
                            "dest_hash": self._snapshots[dest]["files"].get(path)})
        payloads, backups = {}, {}
        for side in ("local", "remote"):
            paths = [x["path"] for x in actions if x["source"] == side or x["dest"] == side]
            if paths:
                expected = {p: self._snapshots[side]["files"].get(p) for p in paths}
                files = self._peers[side].call("read_many", paths=paths, expected_git=self._git_context(side), expected_hashes=expected)["files"]
                for x in actions:
                    if x["source"] == side:
                        payloads[x["path"]] = files[x["path"]]
                    if x["dest"] == side:
                        backups[x["path"]] = files[x["path"]]
        folder = self.state_dir / "journal" / uuid.uuid4().hex
        folder.mkdir(parents=True, mode=0o700)
        journal = {"schema": 1, "status": "prepared", "actions": actions,
                   "completed": [], "created_at": time.time(), "backups": {},
                   "endpoints": self._identity,
                   "expected_git": {s: self._git_context(s) for s in ("local", "remote")}}
        for index, item in enumerate(actions):
            old = backups[item["path"]]
            filename = str(index) + ".bin" if old is not None else None
            if old is not None:
                with (folder / filename).open("wb") as handle:
                    handle.write(base64.b64decode(old["data"], validate=True))
                    handle.flush()
                    os.fsync(handle.fileno())
            journal["backups"][item["path"]] = filename
        _save(folder / "journal.json", journal)
        self._publish(state="syncing")
        failure = None
        try:
            for side in ("local", "remote"):
                batch = [{"path": x["path"], "data": payloads[x["path"]]["data"] if payloads[x["path"]] else None,
                          "expected": x["dest_hash"]} for x in actions if x["dest"] == side]
                if batch:
                    journal["status"] = "writing"
                    _save(folder / "journal.json", journal)
                    # Write RPCs are never retried here; their result may be unknown.
                    self._peers[side].call("write_many", actions=batch, expected_git=self._git_context(side),
                                           allow_delete=self.options["allow_delete"])
        except Exception as exc:
            failure = exc
        finally:
            # Only bytes verified at the destination advance each file baseline.
            # The source may already contain the next editor save at this point.
            self._collect_snapshots(False, barrier=True)
            for item in actions:
                dest = item["dest"]
                if self.view()["connection"][dest]["connected"] and self._git_context(dest) == {"head": self._base["head"], "branch": self._base["branch"]}:
                    if self._snapshots[dest]["files"].get(item["path"]) == item["source_hash"]:
                        journal["completed"].append(item["path"])
                        if item["source_hash"] is None:
                            self._base["files"].pop(item["path"], None)
                        else:
                            self._base["files"][item["path"]] = item["source_hash"]
            self._save_base()
            journal["status"] = "complete" if len(journal["completed"]) == len(actions) else "interrupted"
            if failure is not None:
                journal["error"] = str(failure)
            _save(folder / "journal.json", journal)
            if journal["status"] == "interrupted":
                self._unconfirmed.append((folder / "journal.json", journal))
        if failure is not None:
            raise failure
        if journal["status"] == "complete":
            self._publish(last_sync_at=time.time())
        self._event("info", "Synchronized " + str(len(journal["completed"])) + " files",
                    paths=journal["completed"])

    def _cycle(self, manual=False, observe=False):
        force = time.monotonic() >= self._force_at
        if force:
            self._force_at = time.monotonic() + self.options["reconcile_interval"]
        if not self._collect_snapshots(force, barrier=manual or observe):
            self._publish(state="offline")
            return
        self._publish_observation()
        snap = self._snapshots
        git = {side: {k: snap[side][k] for k in ("head", "branch", "dirty")} for side in snap}
        git.update(match=bool(snap["local"]["branch"]) and self._git_context("local") == self._git_context("remote"), auto_follow=self.options["auto_git"])
        self._publish(git=git, documents={side: dict(s["docs"]) for side, s in snap.items()})
        self._confirm_interrupted()
        if self._base is None:
            if not git["match"] or snap["local"]["files"] != snap["remote"]["files"]:
                self._publish(state="git_blocked" if not git["match"] else "conflict",
                              last_error={"code": "baseline_missing", "message": "Initial baseline requires equal Git identity and selected files"})
                return
            if any(s["unmerged"] or s["git_operations"] for s in snap.values()):
                self._publish(state="git_blocked", last_error={"code": "git_operation", "message": "Finish the active Git operation before establishing a baseline"})
                return
            if any("missing_worktree_file" in s.get("excluded", {}).values() for s in snap.values()):
                self._publish(state="conflict", last_error={"code": "baseline_missing_file", "message": "Resolve tracked worktree deletion before establishing the initial baseline"})
                return
            self._base = {"schema": 1, **self._git_context("local"), "files": dict(snap["local"]["files"]), "endpoints": self._identity}
            self._save_base()
        if self._paused:
            self._publish(state="paused", paused=True)
            return
        preliminary_pending, preliminary_conflicts = self._plan()
        if preliminary_conflicts:
            self._publish(state="conflict", pending=preliminary_pending, conflicts=preliminary_conflicts,
                          files={"local_count": len(snap["local"]["files"]), "remote_count": len(snap["remote"]["files"]),
                                 "pending_count": len(preliminary_pending), "conflict_count": len(preliminary_conflicts)}, last_error=None)
            return
        if not self._reconcile_git(allow_follow=not observe):
            return
        self._advance_equal()
        pending, conflicts = self._plan()
        self._publish(pending=pending, conflicts=conflicts,
                      files={"local_count": len(self._snapshots["local"]["files"]), "remote_count": len(self._snapshots["remote"]["files"]),
                             "pending_count": len(pending), "conflict_count": len(conflicts)},
                      state="conflict" if conflicts else ("pending" if pending else "synced"), last_error=None)
        if pending and not conflicts and not observe and (manual or self.options["auto_sync"]):
            self._apply(pending)
            # Fresh observations are planned again without recursively applying.
            self._cycle_after_write()

    def _cycle_after_write(self):
        if not all(x["connected"] for x in self.view()["connection"].values()):
            return
        self._publish_observation()
        git = {side: {k: self._snapshots[side][k] for k in ("head", "branch", "dirty")} for side in self._snapshots}
        git.update(match=bool(self._snapshots["local"]["branch"]) and self._git_context("local") == self._git_context("remote"),
                   auto_follow=self.options["auto_git"])
        self._publish(git=git, documents={side: dict(s["docs"]) for side, s in self._snapshots.items()})
        expected = {"head": self._base["head"], "branch": self._base["branch"]}
        if (any(self._git_context(s) != expected for s in self._snapshots)
                or any(s["unmerged"] or s["git_operations"] for s in self._snapshots.values())):
            self._publish(state="git_blocked", last_error={"code": "git_changed_during_write", "message": "Git changed or an operation began during file synchronization; fresh observations require review"})
            return
        pending, conflicts = self._plan()
        self._publish(state="conflict" if conflicts else ("pending" if pending else "synced"), pending=pending, conflicts=conflicts,
                      files={"local_count": len(self._snapshots["local"]["files"]), "remote_count": len(self._snapshots["remote"]["files"]),
                             "pending_count": len(pending), "conflict_count": len(conflicts)})

    def _reconcile_git(self, allow_follow=True):
        if any(s["unmerged"] or s["git_operations"] for s in self._snapshots.values()):
            self._publish(state="git_blocked", last_error={"code": "git_operation", "message": "Unmerged index or active Git operation"})
            return False
        expected = {"head": self._base["head"], "branch": self._base["branch"]}
        if all(self._git_context(side) == expected for side in self._snapshots):
            return True
        if not allow_follow:
            self._publish(state="git_blocked", last_error={"code": "git_follow_pending", "message": "Git history changed; observation does not perform Git following"})
            return False
        if not self.options["auto_git"]:
            self._publish(state="git_blocked", last_error={"code": "auto_git_disabled", "message": "Git history changed and automatic following is disabled"})
            return False
        self._publish(state="syncing")
        self._event("info", "Checking and following Git history")
        coordinator = self._coordinator_type(self._peers, self.state_dir, self._identity)
        result = coordinator.reconcile(self._snapshots, self._base)
        self._snapshots = result["snapshots"]
        if result["blocked"]:
            self._publish(state="git_blocked", last_error={"code": "git_blocked", "message": result["reason"]})
            return False
        self._base = result["baseline"]
        self._save_base()
        git = {side: {k: self._snapshots[side][k] for k in ("head", "branch", "dirty")} for side in self._snapshots}
        git.update(match=self._git_context("local") == self._git_context("remote"), auto_follow=self.options["auto_git"])
        self._publish(git=git, documents={side: dict(s["docs"]) for side, s in self._snapshots.items()})
        if result.get("followed"):
            self._event("info", "Safely followed Git history")
        return True

    def _execute_action(self, identifier, name, params):
        if name == "pause":
            self._paused = True
            self._publish(state="paused", paused=True)
        elif name == "resume":
            self._paused = False
            self._publish(paused=False)
            self._cycle()
        elif name == "sync":
            self._cycle(manual=True)
            view = self.view()
            if view["state"] in ("conflict", "git_blocked"):
                return {"ok": False, "error": view["last_error"] or
                        {"code": view["state"], "message": "Synchronization is blocked by " + view["state"].replace("_", " ")}}
        elif name == "observe":
            self._cycle(observe=True)
        elif name == "resolve":
            expected = params["expected_revision"]
            if self.view()["revision"] != expected:
                raise ValueError("stale revision; refresh conflict status")
            # Recheck contents, Git identity, and the revision before any overwrite.
            self._cycle()
            if self.view()["state"] != "conflict":
                raise ValueError("resolution requires current connected conflict observations")
            if self.view()["revision"] != expected:
                raise ValueError("stale revision; files changed before resolution")
            path, source = params["path"], params["choice"]
            if path not in {x["path"] for x in self.view()["conflicts"]}:
                raise ValueError("path is not a current conflict")
            dest = "remote" if source == "local" else "local"
            expected_git = {"head": self._base["head"], "branch": self._base["branch"]}
            if any(self._git_context(s) != expected_git for s in ("local", "remote")):
                raise ValueError("resolve requires matching baseline Git identity")
            reason = next(x["reason"] for x in self.view()["conflicts"] if x["path"] == path)
            if reason in ("case_collision", "selection_changed"):
                raise ValueError("resolve cannot override path or selection safety boundaries")
            sha = self._snapshots[source]["files"].get(path)
            if sha is None and not self.options["allow_delete"]:
                raise ValueError("resolution deletes a file; allow_delete is disabled")
            self._apply([{"path": path, "direction": source + "_to_" + dest}])
            self._cycle_after_write()
        elif name == "checkpoint":
            self._cycle(manual=True)
            if self._base is None or self.view()["state"] != "synced":
                raise ValueError("checkpoint requires synchronized files and Git identity")
            coordinator = self._coordinator_type(self._peers, self.state_dir, self._identity)
            self._publish(state="syncing")
            self._event("info", "Creating explicit Git checkpoint")
            result = coordinator.checkpoint(params["message"], params.get("tag"),
                                            snapshots=self._snapshots, baseline=self._base)
            self._snapshots = result["snapshots"]
            if result["blocked"]:
                error = {"code": "git_blocked", "message": result["reason"]}
                self._publish(state="git_blocked", last_error=error)
                return {"ok": False, "error": error}
            self._base = result["baseline"]
            self._save_base()
            self._cycle()
        self._event("info", name + " completed (" + identifier + ")")
        return {"ok": True}

    def _loop(self):
        try:
            self._initialize()
            while not self._stop.is_set():
                started = time.monotonic()
                try:
                    try:
                        request = self._queue.get_nowait()
                    except queue.Empty:
                        request = None
                    if request is not None:
                        self._action_status(request[0], request[1], "running")
                        if request[1] == "sync":
                            self._publish(state="syncing")
                        outcome = self._execute_action(*request)
                        if outcome["ok"]:
                            self._action_status(request[0], request[1], "complete")
                        else:
                            self._action_status(request[0], request[1], "failed", outcome["error"])
                            self._event("error", request[1] + " blocked: " + outcome["error"]["message"],
                                        action=request[1], action_id=request[0], error=outcome["error"])
                    else:
                        self._cycle()
                except Exception as exc:
                    if request is not None:
                        self._action_status(request[0], request[1], "failed",
                                            {"code": "operation_failed", "message": str(exc)})
                    state = "offline" if not all(v["connected"] for v in self.view()["connection"].values()) else "error"
                    self._publish(state=state, last_error={"code": "operation_failed", "message": str(exc)})
                    self._event("error", str(exc))
                self._publish(performance={"last_cycle_ms": round((time.monotonic() - started) * 1000, 2)})
                self._stop.wait(self.options["poll_interval"])
        except Exception as exc:
            self._initialization_error = str(exc)
            self._publish(state="error", last_error={"code": "initialization_failed", "message": str(exc)})
            self._event("error", str(exc))
            self._stop.wait()
        finally:
            while True:
                try:
                    identifier, name, _ = self._queue.get_nowait()
                except queue.Empty:
                    break
                self._action_status(identifier, name, "failed", {"code": "service_stopped", "message": "Service stopped before executing this action"})
            for peer in list(self._peers.values()):
                peer.close()
            self._peers.clear()
            self._snapshot_workers.shutdown(wait=True, cancel_futures=True)
            if self._worktree_lock is not None:
                self._worktree_lock.release()
            self._controller.release()
