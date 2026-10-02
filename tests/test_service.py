import base64
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import sys
import subprocess
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from worktree_bridge.service import BridgeService, ControllerLock


def digest(value):
    return hashlib.sha256(value).hexdigest()


class FakePeer:
    def __init__(self, spec, store):
        self.side = spec["root"]
        self.store = store
        self.closed = False

    def close(self):
        self.closed = True

    def call(self, op, **args):
        side = self.side
        if self.store.get("offline") == side:
            raise RuntimeError("disconnected")
        if op == "claim_controller":
            return {"claimed": True, "instance_id": args["instance_id"]}
        if op == "snapshot":
            concurrent_gate = self.store.get("concurrent_gate")
            if concurrent_gate is not None:
                concurrent_gate.wait(timeout=3)
            on_observation = self.store.get("on_observation")
            if on_observation is not None:
                on_observation()
            gate = self.store.get("gate")
            if gate is not None:
                gate.wait(2)
            result = {"head": "abc", "branch": "main", "dirty": True,
                    "files": {p: digest(b) for p, b in self.store[side].items()},
                    "docs": {p: digest(b) for p, b in self.store[side].items() if Path(p).name in ("AGENTS.md", "CONTEXT.md")},
                    "unmerged": [], "git_operations": [], "excluded": {},
                    "observed_at": time.time(), "revision": "x", "watcher": "poll"}
            if side == "remote" and self.store["writes"]:
                result.update(self.store.get("post_write_git", {}))
            result.update(self.store.get("snapshot_git", {}).get(side, {}))
            self.store.setdefault("snapshot_args", []).append(args)
            return result
        if op == "read_many":
            files = {}
            for path in args["paths"]:
                data = self.store[side].get(path)
                sha = digest(data) if data is not None else None
                if sha != args["expected_hashes"][path]:
                    raise ValueError("CAS changed")
                files[path] = {"sha256": sha, "data": base64.b64encode(data).decode()} if data is not None else None
            return {"files": files}
        if op == "write_many":
            self.store["writes"] += 1
            for item in args["actions"]:
                current = self.store[side].get(item["path"])
                if (digest(current) if current is not None else None) != item["expected"]:
                    raise ValueError("CAS changed")
            for item in args["actions"]:
                if item["data"] is None:
                    if not args["allow_delete"]:
                        raise ValueError("delete disabled")
                    self.store[side].pop(item["path"], None)
                else:
                    self.store[side][item["path"]] = base64.b64decode(item["data"])
            if self.store.pop("next_save", False):
                self.store["local"]["a.txt"] = b"next-save"
            if self.store.pop("unknown_write", False):
                raise RuntimeError("reply lost")
            if self.store.pop("offline_write", False):
                self.store["offline"] = side
                raise RuntimeError("reply lost and disconnected")
            return {"applied": [x["path"] for x in args["actions"]]}
        raise AssertionError(op)


class FakeService(BridgeService):
    def _initialize(self):
        self._identity = {"fake": True}
        self._specs = {s: {"root": s} for s in ("local", "remote")}
        path = self.state_dir / "baseline.json"
        if path.exists():
            self._base = json.loads(path.read_text(encoding="utf-8"))


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "config.json"
        self.store = {"local": {"a.txt": b"base"}, "remote": {"a.txt": b"base"}, "writes": 0}

    def service(self, **options):
        self.config.write_text(json.dumps({"local_root": str(self.root / "local"),
                                          "remote": {"kind": "local", "root": str(self.root / "remote")},
                                          "state_dir": str(self.root / "state"),
                                          "service": {"poll_interval": .02, **options}}), encoding="utf-8")
        result = FakeService(self.config, peer_factory=lambda spec: FakePeer(spec, self.store))
        result._initialize()
        self.addCleanup(result._snapshot_workers.shutdown, wait=True, cancel_futures=True)
        return result

    def test_unknown_initial_state_then_equal_baseline(self):
        service = self.service()
        self.assertEqual(service.view()["state"], "initializing")
        self.assertFalse(service.view()["connection"]["local"]["connected"])
        service._cycle()
        self.assertEqual(service.view()["state"], "synced")
        self.assertTrue((service.state_dir / "baseline.json").exists())

    def test_service_interval_options_require_finite_positive_numbers(self):
        for field in ("poll_interval", "reconcile_interval", "stale_after_seconds"):
            for value in (None, "0.25", True, False, float("nan"), float("inf"), float("-inf"), 0, -1):
                with self.subTest(field=field, value=value):
                    with self.assertRaisesRegex(ValueError, field):
                        self.service(**{field: value})
        with self.assertRaisesRegex(ValueError, "stale_after_seconds"):
            self.service(stale_after_seconds=.5)

    def test_service_port_and_authorization_options_validate_json_types(self):
        for value in (-1, 65536, 1.0, True, "0", None):
            with self.subTest(port=value):
                with self.assertRaisesRegex(ValueError, "port"):
                    self.service(port=value)
        for field in ("auto_sync", "auto_git", "allow_delete"):
            for value in (0, 1, "false", None):
                with self.subTest(field=field, value=value):
                    with self.assertRaisesRegex(ValueError, field):
                        self.service(**{field: value})
        service = self.service(poll_interval=.01, reconcile_interval=1, stale_after_seconds=1,
                               port=65535, auto_sync=False, auto_git=False, allow_delete=True)
        self.assertEqual(service.view()["state"], "initializing")
        self.assertEqual(service._peers, {})

    def test_initial_divergence_cannot_make_baseline(self):
        service = self.service()
        self.store["remote"]["a.txt"] = b"diverged"
        service._cycle()
        self.assertEqual(service.view()["state"], "conflict")
        self.assertFalse((service.state_dir / "baseline.json").exists())
        self.assertEqual(self.store["writes"], 0)

    def test_real_conflict_blocks_entire_batch(self):
        service = self.service()
        service._cycle()
        self.store["local"].update({"a.txt": b"left", "b.txt": b"pending"})
        self.store["remote"]["a.txt"] = b"right"
        service._cycle()
        self.assertEqual(service.view()["state"], "conflict")
        self.assertEqual(service.view()["files"]["pending_count"], 1)
        self.assertNotIn("b.txt", self.store["remote"])
        self.assertEqual(self.store["writes"], 0)

    def test_consecutive_source_save_advances_verified_file_baseline(self):
        service = self.service()
        service._cycle()
        self.store["local"]["a.txt"] = b"first-save"
        self.store["next_save"] = True
        service._cycle()
        self.assertEqual(service._base["files"]["a.txt"], digest(b"first-save"))
        self.assertEqual(service.view()["state"], "pending")
        service._cycle()
        self.assertEqual(self.store["remote"]["a.txt"], b"next-save")
        self.assertEqual(service.view()["state"], "synced")

    def test_lost_write_reply_is_inspected_and_not_replayed(self):
        service = self.service()
        service._cycle()
        self.store["local"]["a.txt"] = b"saved"
        self.store["unknown_write"] = True
        with self.assertRaisesRegex(RuntimeError, "reply lost"):
            service._cycle()
        self.assertEqual(service._base["files"]["a.txt"], digest(b"saved"))
        service._cycle()
        self.assertEqual(self.store["writes"], 1)
        self.assertEqual(service.view()["state"], "synced")
        journal = next((service.state_dir / "journal").glob("*/journal.json"))
        self.assertEqual(json.loads(journal.read_text())["status"], "complete")

    def test_delete_requires_enabled_option(self):
        service = self.service()
        service._cycle()
        del self.store["local"]["a.txt"]
        service._cycle()
        self.assertEqual(service.view()["conflicts"][0]["reason"], "delete_requires_allow_delete")
        self.assertIn("a.txt", self.store["remote"])
        service.options["allow_delete"] = True
        service._cycle()
        self.assertNotIn("a.txt", self.store["remote"])

    def test_git_change_after_write_never_reports_synced(self):
        for metadata in ({"branch": "other"}, {"git_operations": ["MERGE_HEAD"]}):
            with self.subTest(metadata=metadata):
                self.store = {"local": {"a.txt": b"base"}, "remote": {"a.txt": b"base"}, "writes": 0}
                # Use a fresh state so the subcases establish independent bases.
                old_state = self.root / "state" / "baseline.json"
                old_state.unlink(missing_ok=True)
                service = self.service()
                service._cycle()
                self.store["local"]["a.txt"] = b"edit"
                self.store["post_write_git"] = metadata
                service._cycle()
                self.assertEqual(service.view()["state"], "git_blocked")
                if "branch" in metadata:
                    self.assertEqual(service.view()["git"]["remote"]["branch"], "other")
                    self.assertFalse(service.view()["git"]["match"])

    def test_write_confirmation_uses_content_barrier_and_caches_document_hashes(self):
        service = self.service()
        service._cycle()
        self.store["local"]["AGENTS.md"] = b"Read the task context"
        service._cycle()
        self.assertEqual(service.view()["state"], "synced")
        self.assertEqual(service.view()["documents"], {side: {"AGENTS.md": digest(b"Read the task context")} for side in ("local", "remote")})
        confirmation = self.store["snapshot_args"][-2:]
        self.assertTrue(all(args.get("barrier") for args in confirmation))
        self.assertFalse(any(args["force"] for args in confirmation))

    def test_observe_barrier_does_not_sync_or_follow(self):
        service = self.service(auto_sync=False)
        service._cycle()
        self.store["local"]["a.txt"] = b"edited"
        service._cycle(observe=True)
        self.assertEqual(service.view()["state"], "pending")
        self.assertEqual(self.store["writes"], 0)
        self.assertTrue(self.store["snapshot_args"][-1]["barrier"])
        self.assertFalse(self.store["snapshot_args"][-1]["force"])

    def test_queued_observation_has_explicit_completion_receipt(self):
        service = self.service(auto_sync=False).start()
        try:
            deadline = time.monotonic() + 3
            while service.view()["state"] != "synced" and time.monotonic() < deadline:
                time.sleep(.01)
            self.store["local"]["a.txt"] = b"new"
            accepted = service.action("observe")
            self.assertTrue(accepted["queued"])
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                receipt = next((r for r in service.view()["recent_actions"] if r["id"] == accepted["id"]), None)
                if receipt and receipt["status"] == "complete":
                    break
                time.sleep(.01)
            else:
                self.fail(str(service.view()))
            self.assertEqual(receipt["name"], "observe")
            self.assertIsInstance(receipt["completed_at"], float)
            self.assertEqual(service.view()["state"], "pending")
            self.assertEqual(self.store["writes"], 0)
        finally:
            service.stop()
            service.join(3)

    def queued_receipt(self, service, name, params=None):
        accepted = service.action(name, params)
        self.assertTrue(accepted["ok"])
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            receipt = next((r for r in service.view()["recent_actions"] if r["id"] == accepted["id"]), None)
            if receipt and receipt["status"] in ("complete", "failed"):
                return receipt
            time.sleep(.01)
        self.fail(str(service.view()))

    def test_queued_checkpoint_blocked_receipts_preserve_git_blocked_state(self):
        for reason in ("tag already exists", "staged work blocks Git coordination"):
            with self.subTest(reason=reason):
                service = self.service(auto_sync=False)
                service._cycle()
                gate = threading.Event()

                class BlockedCoordinator:
                    def __init__(self, peers, state_dir, identity):
                        pass

                    def checkpoint(inner, message, tag, snapshots, baseline):
                        self.store["gate"] = gate
                        return {"blocked": True, "reason": reason, "snapshots": snapshots, "baseline": baseline}

                service._coordinator_type = BlockedCoordinator
                service.start()
                try:
                    receipt = self.queued_receipt(service, "checkpoint", {"message": "explicit checkpoint", "tag": "existing-tag"})
                    self.assertEqual(receipt["status"], "failed")
                    self.assertEqual(receipt["error"], {"code": "git_blocked", "message": reason})
                    self.assertEqual(service.view()["state"], "git_blocked")
                    self.assertFalse(any(e["message"].startswith("checkpoint completed") for e in service.view()["events"]))
                finally:
                    service.stop()
                    gate.set()
                    service.join(3)
                    self.store.pop("gate", None)

    def test_queued_checkpoint_preflight_rejections_are_failed_receipts(self):
        for reason in ("tag already exists", "staged work blocks Git coordination"):
            with self.subTest(reason=reason):
                service = self.service(auto_sync=False)
                service._cycle()
                gate = threading.Event()

                class RefusingCoordinator:
                    def __init__(self, peers, state_dir, identity):
                        pass

                    def checkpoint(inner, message, tag, snapshots, baseline):
                        self.store["gate"] = gate
                        raise ValueError(reason)

                service._coordinator_type = RefusingCoordinator
                service.start()
                try:
                    receipt = self.queued_receipt(service, "checkpoint", {"message": "explicit checkpoint", "tag": "existing-tag"})
                    self.assertEqual(receipt["status"], "failed")
                    self.assertIn(reason, receipt["error"]["message"])
                    self.assertEqual(self.store["writes"], 0)
                finally:
                    service.stop()
                    gate.set()
                    service.join(3)
                    self.store.pop("gate", None)

    def test_queued_sync_conflict_fails_but_observation_completes(self):
        service = self.service(auto_sync=False)
        service._cycle()
        self.store["local"]["a.txt"] = b"left"
        self.store["remote"]["a.txt"] = b"right"
        service.start()
        try:
            sync_receipt = self.queued_receipt(service, "sync")
            self.assertEqual(sync_receipt["status"], "failed")
            self.assertEqual(sync_receipt["error"]["code"], "conflict")
            self.assertEqual(service.view()["state"], "conflict")
            observation = self.queued_receipt(service, "observe")
            self.assertEqual(observation["status"], "complete")
            self.assertEqual(service.view()["state"], "conflict")
            self.assertEqual(self.store["writes"], 0)
        finally:
            service.stop()
            service.join(3)

    def test_queued_sync_git_operation_block_is_a_failed_receipt(self):
        service = self.service(auto_sync=False)
        service._cycle()
        self.store["snapshot_git"] = {"remote": {"git_operations": ["MERGE_HEAD"]}}
        service.start()
        try:
            receipt = self.queued_receipt(service, "sync")
            self.assertEqual(receipt["status"], "failed")
            self.assertEqual(receipt["error"]["code"], "git_operation")
            self.assertEqual(service.view()["state"], "git_blocked")
            self.assertEqual(self.store["writes"], 0)
        finally:
            service.stop()
            service.join(3)

    def test_reconnect_confirms_unknown_write_before_planning_next_save(self):
        service = self.service()
        service._cycle()
        self.store["local"]["a.txt"] = b"first-save"
        self.store["offline_write"] = True
        self.store["next_save"] = True
        with self.assertRaisesRegex(RuntimeError, "reply lost"):
            service._cycle()
        self.assertEqual(service.view()["state"], "offline")
        del self.store["offline"]
        service._retry_at["remote"] = 0
        service._cycle()
        self.assertEqual(self.store["remote"]["a.txt"], b"next-save")
        self.assertEqual(service.view()["state"], "synced")
        self.assertEqual(self.store["writes"], 2)

    def test_auto_sync_disabled_manual_sync_and_pause(self):
        service = self.service(auto_sync=False)
        service._cycle()
        self.store["local"]["a.txt"] = b"new"
        service._cycle()
        self.assertEqual(service.view()["state"], "pending")
        service._execute_action("pause-1", "pause", {})
        service._cycle(manual=True)
        self.assertEqual(service.view()["state"], "paused")
        self.assertEqual(self.store["writes"], 0)
        service._execute_action("resume-1", "resume", {})
        service._cycle(manual=True)
        self.assertEqual(self.store["remote"]["a.txt"], b"new")

    def test_revision_stable_across_heartbeat_and_stale_resolve_rejected(self):
        service = self.service()
        service._cycle()
        revision = service.view()["revision"]
        service._cycle()
        self.assertEqual(service.view()["revision"], revision)
        self.store["local"]["a.txt"] = b"left"
        self.store["remote"]["a.txt"] = b"right"
        service._cycle()
        result = service.action("resolve", {"path": "a.txt", "choice": "local", "expected_revision": revision})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "stale_revision")
        params = {"path": "a.txt", "choice": "local", "expected_revision": service.view()["revision"]}
        service._execute_action("resolution", "resolve", params)
        self.assertEqual(self.store["remote"]["a.txt"], b"left")

    def test_cache_is_fast_during_slow_scan(self):
        service = self.service()
        gate = threading.Event()
        self.store["gate"] = gate
        worker = threading.Thread(target=service._cycle)
        worker.start()
        try:
            started = time.monotonic()
            self.assertEqual(service.view()["state"], "initializing")
            self.assertTrue(service.action("pause")["queued"])
            self.assertLess(time.monotonic() - started, .1)
        finally:
            gate.set()
            worker.join(3)

    def test_snapshot_workers_enter_both_peers_before_either_returns(self):
        service = self.service()
        # A sequential implementation cannot pass this rendezvous. No elapsed
        # time threshold is needed to prove the two independent reads overlap.
        gate = threading.Barrier(2)
        self.store["concurrent_gate"] = gate
        service._cycle()
        self.assertFalse(gate.broken)
        self.assertEqual(service.view()["state"], "synced")
        self.assertTrue(service.view()["connection"]["local"]["connected"])
        self.assertTrue(service.view()["connection"]["remote"]["connected"])

    def test_disconnect_marks_offline_and_retains_last_seen(self):
        service = self.service()
        service._cycle()
        seen = service.view()["connection"]["remote"]["last_seen"]
        self.store["offline"] = "remote"
        service._cycle()
        self.assertEqual(service.view()["state"], "offline")
        self.assertEqual(service.view()["connection"]["remote"], {"connected": False, "last_seen": seen})
        del self.store["offline"]
        service._retry_at["remote"] = 0
        service._cycle()
        self.assertEqual(service.view()["state"], "synced")

    def test_stale_synced_cache_is_never_green(self):
        service = self.service()
        service._cycle()
        old_time = time.time() - 11
        connections = service.view()["connection"]
        for connection in connections.values():
            connection["last_seen"] = old_time
        service._publish(observed_at=old_time, connection=connections)
        result = service.view()
        self.assertEqual(result["state"], "checking")
        self.assertTrue(result["stale_observation"])
        self.assertEqual(result["last_error"]["code"], "stale_observation")
        self.assertTrue(result["connection"]["remote"]["connected"])
        self.assertEqual(result["connection"]["remote"]["last_seen"], old_time)

    def test_peer_clock_offsets_do_not_control_freshness(self):
        for peer_time in (400, 1600):
            with self.subTest(peer_time=peer_time):
                service = self.service()
                self.store["snapshot_git"] = {"remote": {"observed_at": peer_time}}
                with mock.patch("worktree_bridge.service.time.time", return_value=1000):
                    service._cycle()
                    view = service.view()
                self.assertEqual(view["state"], "synced")
                self.assertFalse(view["stale_observation"])
                self.assertEqual(view["observed_at"], 1000)
                self.assertEqual(view["connection"]["remote"]["last_seen"], 1000)
                self.assertEqual(service._snapshots["remote"]["observed_at"], peer_time)
                self.assertEqual(service._snapshots["remote"]["controller_observed_at"], 1000)

    def test_long_observation_uses_start_time_for_conservative_freshness(self):
        service = self.service()
        clock = [1000]
        self.store["concurrent_gate"] = threading.Barrier(2)
        self.store["on_observation"] = lambda: clock.__setitem__(0, 1012)
        with mock.patch("worktree_bridge.service.time.time", side_effect=lambda: clock[0]):
            service._cycle()
            view = service.view()
        self.assertEqual(view["state"], "checking")
        self.assertTrue(view["stale_observation"])
        self.assertEqual(view["observed_at"], 1000)
        self.assertTrue(all(c["last_seen"] == 1012 for c in view["connection"].values()))
        self.assertTrue(all(c["connected"] for c in view["connection"].values()))

    def test_untimed_git_snapshots_preserve_previous_observation_time(self):
        service = self.service()
        with mock.patch("worktree_bridge.service.time.time", return_value=1000):
            service._cycle()
        for snapshot in service._snapshots.values():
            snapshot.pop("controller_observed_at")
            snapshot["observed_at"] = 100000
        with mock.patch("worktree_bridge.service.time.time", return_value=1012):
            service._cycle_after_write()
            view = service.view()
        self.assertEqual(view["observed_at"], 1000)
        self.assertEqual(view["state"], "checking")

    def test_controller_lock_rejects_second_owner_and_releases(self):
        first, second = ControllerLock(self.root), ControllerLock(self.root)
        first.acquire()
        try:
            with self.assertRaisesRegex(RuntimeError, "another controller"):
                second.acquire()
        finally:
            first.release()
        second.acquire()
        second.release()


class NativeServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.local, self.remote = self.root / "local", self.root / "remote"
        self.local.mkdir()
        self.git(self.local, "init", "-q", "-b", "main")
        self.git(self.local, "config", "user.email", "test@example.com")
        self.git(self.local, "config", "user.name", "Test")
        (self.local / "a.txt").write_text("base", encoding="utf-8")
        self.git(self.local, "add", "a.txt")
        self.git(self.local, "commit", "-qm", "initial")
        self.git(self.root, "clone", "-q", str(self.local), str(self.remote))
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({"local_root": str(self.local), "remote": {"kind": "local", "root": str(self.remote)},
                                          "state_dir": str(self.root / "state"), "service": {"poll_interval": .03}}), encoding="utf-8")
        self.services = []
        self.addCleanup(self.cleanup_services)

    def git(self, directory, *args):
        subprocess.run(["git", "-C", str(directory), *args], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def cleanup_services(self):
        for service in self.services:
            service.stop()
            service.join(10)

    def wait_state(self, service, state, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            view = service.view()
            if view["state"] == state:
                return view
            if view["state"] == "error":
                self.fail(str(view["last_error"]))
            time.sleep(.03)
        self.fail("State did not become " + state + ": " + str(service.view()))

    def test_existing_baseline_identity_and_native_sync(self):
        from worktree_bridge.__main__ import configuration, endpoint_identity, make_baseline, snapshots, save_json
        local, remote, state = configuration(self.config)
        old = make_baseline(snapshots(local, remote), endpoint_identity(local, remote))
        save_json(state / "baseline.json", old)
        (self.local / "a.txt").write_text("edited", encoding="utf-8")
        service = BridgeService(self.config).start()
        self.services.append(service)
        self.wait_state(service, "synced")
        self.assertEqual((self.remote / "a.txt").read_text(), "edited")
        new = json.loads((state / "baseline.json").read_text())
        self.assertEqual(new["endpoints"], old["endpoints"])
        self.assertEqual(new["files"]["a.txt"], digest(b"edited"))

    def test_same_worktree_different_state_rejects_second_controller(self):
        service = BridgeService(self.config).start()
        self.services.append(service)
        self.wait_state(service, "synced")
        config = json.loads(self.config.read_text())
        config["state_dir"] = str(self.root / "other-state")
        other_path = self.root / "other-config.json"
        other_path.write_text(json.dumps(config), encoding="utf-8")
        other = BridgeService(other_path).start()
        self.services.append(other)
        deadline = time.monotonic() + 5
        while other.view()["state"] == "initializing" and time.monotonic() < deadline:
            time.sleep(.03)
        self.assertEqual(other.view()["state"], "error")
        self.assertIn("another controller", other.view()["last_error"]["message"])

    def test_follow_existing_commit_then_explicit_checkpoint(self):
        service = BridgeService(self.config).start()
        self.services.append(service)
        first = self.wait_state(service, "synced")
        service.action("pause")
        self.wait_state(service, "paused")
        (self.local / "a.txt").write_text("commit-content", encoding="utf-8")
        self.git(self.local, "add", "a.txt")
        self.git(self.local, "commit", "-qm", "next")
        service.action("resume")
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            view = service.view()
            if view["state"] == "synced" and view["git"]["local"]["head"] != first["git"]["local"]["head"]:
                break
            time.sleep(.05)
        else:
            self.fail(str(service.view()))
        self.assertEqual(view["git"]["local"]["head"], view["git"]["remote"]["head"])
        self.assertEqual((self.remote / "a.txt").read_text(), "commit-content")
        (self.local / "a.txt").write_text("checkpoint-content", encoding="utf-8")
        deadline = time.monotonic() + 10
        while (self.remote / "a.txt").read_text() != "checkpoint-content" and time.monotonic() < deadline:
            time.sleep(.05)
        self.wait_state(service, "synced")
        before = service.view()["git"]["local"]["head"]
        self.assertTrue(service.action("checkpoint", {"message": "explicit checkpoint", "tag": "experiment-v1"})["ok"])
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            view = service.view()
            if view["state"] == "error":
                self.fail(str(view["last_error"]))
            if view["state"] == "synced" and view["git"]["local"]["head"] != before:
                break
            time.sleep(.05)
        else:
            self.fail(str(service.view()))
        self.assertEqual(view["git"]["local"]["head"], view["git"]["remote"]["head"])
        self.git(self.remote, "rev-parse", "--verify", "refs/tags/experiment-v1")

    def test_shared_remote_lease_blocks_other_local_controller_then_releases(self):
        first = BridgeService(self.config).start()
        self.services.append(first)
        self.wait_state(first, "synced")
        local2 = self.root / "local2"
        self.git(self.root, "clone", "-q", str(self.local), str(local2))
        config = json.loads(self.config.read_text())
        config.update(local_root=str(local2), state_dir=str(self.root / "other-state"))
        config_path = self.root / "other-config.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        second = BridgeService(config_path).start()
        self.services.append(second)
        view = self.wait_state(second, "offline")
        self.assertIn("controller", view["last_error"]["message"])
        self.assertEqual((self.remote / "a.txt").read_text(), "base")
        first.stop()
        first.join(10)
        self.wait_state(second, "synced")
        (local2 / "a.txt").write_text("after-release", encoding="utf-8")
        deadline = time.monotonic() + 15
        while (self.remote / "a.txt").read_text() != "after-release" and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertEqual((self.remote / "a.txt").read_text(), "after-release")


if __name__ == "__main__":
    unittest.main()
