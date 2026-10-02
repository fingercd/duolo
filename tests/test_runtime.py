import base64
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from duolo import agent
from duolo.runtime_agent import Runtime


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        (self.root / "code.py").write_bytes(b"original\n")
        (self.root / ".gitignore").write_text("ignored/\n", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-m", "initial")
        self.runtime = Runtime(str(self.root))
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.runtime.close)

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), *args], check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout

    def expected(self):
        snap = self.runtime.snapshot()
        return {"head": snap["head"], "branch": snap["branch"]}, snap

    def test_warm_snapshot_no_git_or_hash_and_stable_revision(self):
        first = self.runtime.snapshot()
        # Let initial watch events drain; signatures remain the correctness guard.
        with mock.patch.object(agent, "git", wraps=agent.git) as git, mock.patch.object(agent, "digest", wraps=agent.digest) as digest:
            second = self.runtime.snapshot()
            third = self.runtime.snapshot()
        self.assertEqual(first["revision"], second["revision"])
        self.assertEqual(second["revision"], third["revision"])
        self.assertEqual(git.call_count, 0)
        self.assertEqual(digest.call_count, 0)

    def test_edit_add_delete_rename(self):
        before = self.runtime.snapshot()
        (self.root / "code.py").write_bytes(b"modified\n")
        edited = self.runtime.snapshot()
        self.assertNotEqual(before["files"]["code.py"], edited["files"]["code.py"])
        (self.root / "new.py").write_bytes(b"new\n")
        self.assertIn("new.py", self.runtime.snapshot()["files"])
        (self.root / "new.py").rename(self.root / "renamed.py")
        snap = self.runtime.snapshot()
        self.assertNotIn("new.py", snap["files"])
        self.assertIn("renamed.py", snap["files"])
        (self.root / "code.py").unlink()
        self.assertNotIn("code.py", self.runtime.snapshot()["files"])

    def test_batch_read_write_delete_and_preflight(self):
        expected, snap = self.expected()
        data = self.runtime.read_many(["code.py", "absent.py"], expected,
                                      {"code.py": snap["files"]["code.py"], "absent.py": None})
        self.assertIsNone(data["files"]["absent.py"])
        self.assertEqual(base64.b64decode(data["files"]["code.py"]["data"]), b"original\n")
        actions = [{"path": "code.py", "data": base64.b64encode(b"changed").decode(), "expected": snap["files"]["code.py"]},
                   {"path": "nested/new.py", "data": base64.b64encode(b"added").decode(), "expected": None}]
        invalid = [{**actions[0]}, {**actions[1], "expected": "stale"}]
        with self.assertRaisesRegex(ValueError, "destination changed"):
            self.runtime.write_many(invalid, expected)
        self.assertEqual((self.root / "code.py").read_bytes(), b"original\n")
        self.assertFalse((self.root / "nested").exists())
        self.assertEqual(self.runtime.write_many(actions, expected)["applied"], ["code.py", "nested/new.py"])
        snap = self.runtime.snapshot()
        delete = [{"path": "nested/new.py", "data": None, "expected": snap["files"]["nested/new.py"]}]
        with self.assertRaisesRegex(ValueError, "allow_delete"):
            self.runtime.write_many(delete, expected)
        self.runtime.write_many(delete, expected, allow_delete=True)
        self.assertFalse((self.root / "nested/new.py").exists())

    def test_ignore_index_and_branch_invalidation(self):
        expected, snap = self.expected()
        (self.root / "new.py").write_bytes(b"new")
        self.assertIn("new.py", self.runtime.snapshot()["files"])
        (self.root / ".gitignore").write_text("ignored/\nnew.py\n", encoding="utf-8")
        self.assertNotIn("new.py", self.runtime.snapshot()["files"])
        with self.assertRaisesRegex(ValueError, "selection|ignored"):
            self.runtime.write_many([{"path": "new.py", "data": "YQ==", "expected": agent.digest(b"new")}], expected)
        (self.root / "other.py").write_bytes(b"other")
        self.assertIn("other.py", self.runtime.snapshot()["files"])
        (self.root / ".git/info/exclude").write_text("other.py\n", encoding="utf-8")
        self.assertNotIn("other.py", self.runtime.snapshot()["files"])
        self.git("checkout", "-b", "other")
        self.assertEqual(self.runtime.snapshot()["branch"], "other")
        with self.assertRaisesRegex(ValueError, "branch/HEAD"):
            self.runtime.read_many(["code.py"], expected, {"code.py": snap["files"]["code.py"]})

    def test_unsafe_ignored_and_oversized_paths(self):
        expected, _ = self.expected()
        for path in ("../escape.py", ".git/config", "ignored/new.py", "secret-token.py", "C:/file.py"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.runtime.write_many([{"path": path, "data": "YQ==", "expected": None}], expected)
        with self.assertRaisesRegex(ValueError, "large"):
            self.runtime.write_many([{"path": "huge.py", "data": base64.b64encode(b"x" * (agent.MAX_BYTES + 1)).decode(), "expected": None}], expected)
        self.assertFalse((self.root / "ignored").exists())

    def test_scan_failure_is_not_empty_success(self):
        self.runtime.snapshot()
        (self.root / "code.py").write_bytes(b"changed")
        with mock.patch.object(self.runtime, "_read_snapshot_bytes", side_effect=PermissionError("denied")):
            with self.assertRaisesRegex(OSError, "cannot inspect/read"):
                self.runtime.snapshot()

    def test_root_is_fixed(self):
        with self.assertRaisesRegex(ValueError, "fixed"):
            self.runtime.dispatch({"root": str(self.root / "other"), "op": "snapshot"})

    def test_poll_reconcile_discovers_deep_new_paths(self):
        self.runtime.snapshot()
        self.runtime.watcher.close()
        self.runtime.watcher.mode = "poll"
        (self.root / "deep").mkdir()
        (self.root / "deep/more").mkdir()
        (self.root / "deep/more/new.py").write_bytes(b"new")
        self.runtime._last_reconcile = 0
        self.assertIn("deep/more/new.py", self.runtime.snapshot()["files"])

    def test_native_hint_discovers_file_in_previously_empty_directory(self):
        (self.root / "empty").mkdir()
        self.runtime.snapshot()
        self.assertNotIn(self.root / "empty", self.runtime._directories)
        (self.root / "empty/new.py").write_bytes(b"new")
        self.runtime.watcher._hint(self.root / "empty/new.py")
        self.assertIn("empty/new.py", self.runtime.snapshot()["files"])

    def test_native_watcher_emits_file_changes(self):
        if self.runtime.watcher.mode == "poll":
            self.skipTest("native watcher unavailable on this platform")
        self.runtime.watcher.drain()
        path = self.root / "native.py"
        path.write_bytes(b"new")
        deadline = time.monotonic() + 2
        seen = set()
        while time.monotonic() < deadline:
            changed, overflow = self.runtime.watcher.drain()
            seen.update(changed)
            if str(path) in seen:
                break
            time.sleep(0.02)
        self.assertIn(str(path), seen)

    def test_git_change_during_scan_fails_then_recovers(self):
        self.runtime.snapshot()
        (self.root / "code.py").write_bytes(b"changed")
        read = self.runtime._read_snapshot_bytes
        def switch(rel, target, expected):
            data = read(rel, target, expected)
            self.git("checkout", "-b", "during-scan")
            return data
        with mock.patch.object(self.runtime, "_read_snapshot_bytes", side_effect=switch):
            with self.assertRaisesRegex(ValueError, "Git metadata changed"):
                self.runtime.snapshot()
        self.assertEqual(self.runtime.snapshot()["branch"], "during-scan")

    def test_batch_overlap_rejected_without_creating_paths(self):
        expected, _ = self.expected()
        with self.assertRaisesRegex(ValueError, "overlap"):
            self.runtime.write_many([{"path": "parent", "data": "YQ==", "expected": None},
                                     {"path": "parent/child.py", "data": "YQ==", "expected": None}], expected)
        self.assertFalse((self.root / "parent").exists())

    def test_symlink_and_nested_repository_are_rejected(self):
        expected, _ = self.expected()
        try:
            (self.root / "linked.py").symlink_to(self.root / "code.py")
        except OSError:
            pass
        else:
            with self.assertRaisesRegex(ValueError, "non-regular|selection"):
                self.runtime.write_many([{"path": "linked.py", "data": "YQ==", "expected": None}], expected)
            self.assertNotIn("linked.py", self.runtime.snapshot()["files"])
        nested = self.root / "nested"
        nested.mkdir()
        subprocess.run(["git", "-C", str(nested), "init"], check=True, capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        with self.assertRaisesRegex(ValueError, "nested git"):
            self.runtime.write_many([{"path": "nested/new.py", "data": "YQ==", "expected": None}], expected)

    def test_warm_batch_read_does_not_spawn_git_per_file(self):
        expected, snap = self.expected()
        with mock.patch.object(agent, "git_process", wraps=agent.git_process) as git:
            self.runtime.read_many(["code.py", ".gitignore"], expected,
                                   {path: snap["files"][path] for path in ("code.py", ".gitignore")})
        self.assertEqual(git.call_count, 0)

    def test_force_and_reconcile_hash_even_when_file_stat_is_restored(self):
        first = self.runtime.snapshot()
        self.runtime.watcher.close()
        self.runtime.watcher.drain()
        path = self.root / "code.py"
        old = path.stat()
        path.write_bytes(b"altered!\n")
        os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns))
        force = self.runtime.snapshot(force=True)
        self.assertNotEqual(first["files"]["code.py"], force["files"]["code.py"])
        path.write_bytes(b"original\n")
        os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns))
        self.runtime._last_reconcile = 0
        reconciled = self.runtime.snapshot()
        self.assertEqual(first["files"]["code.py"], reconciled["files"]["code.py"])

    def test_barrier_rehashes_with_only_selection_git_refresh(self):
        self.runtime.snapshot()
        reconciled_at = self.runtime._last_reconcile
        self.runtime.watcher.close()
        self.runtime.watcher.drain()
        with mock.patch.object(agent, "digest", wraps=agent.digest) as digest, mock.patch.object(agent, "git", wraps=agent.git) as git:
            self.runtime.snapshot(barrier=True)
        self.assertEqual(digest.call_count, 2)
        self.assertEqual(git.call_count, 2)
        self.assertEqual(self.runtime._last_reconcile, reconciled_at)

    def test_barrier_detects_restored_mtime_without_native_events(self):
        first = self.runtime.snapshot()
        self.runtime.watcher.close()
        self.runtime.watcher.drain()
        path = self.root / "code.py"
        old = path.stat()
        path.write_bytes(b"altered!\n")
        os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns))
        barrier = self.runtime.snapshot(barrier=True)
        self.assertNotEqual(first["files"]["code.py"], barrier["files"]["code.py"])

    def test_own_ignore_update_can_share_batch_with_selected_files(self):
        expected, snap = self.expected()
        actions = [{"path": ".gitignore", "data": base64.b64encode(b"ignored/\nother/\n").decode(), "expected": snap["files"][".gitignore"]},
                   {"path": "code.py", "data": "Y2hhbmdlZA==", "expected": snap["files"]["code.py"]}]
        result = self.runtime.write_many(actions, expected)
        self.assertEqual(result["applied"], [".gitignore", "code.py"])
        self.assertEqual((self.root / "code.py").read_bytes(), b"changed")

    def test_ignore_change_after_preflight_blocks_write(self):
        expected, _ = self.expected()
        check = self.runtime._check_new_ignores
        def change(paths):
            check(paths)
            (self.root / ".gitignore").write_text("new.py\n", encoding="utf-8")
        with mock.patch.object(self.runtime, "_check_new_ignores", side_effect=change):
            with self.assertRaisesRegex(RuntimeError, "ignore rules changed"):
                self.runtime.write_many([{"path": "new.py", "data": "YQ==", "expected": None}], expected)
        self.assertFalse((self.root / "new.py").exists())

    def test_index_lock_is_reported_and_blocks_file_writes(self):
        expected, _ = self.expected()
        (self.root / ".git/index.lock").write_bytes(b"recovery-lock")
        self.assertIn("index.lock", self.runtime.snapshot()["git_operations"])
        with self.assertRaisesRegex(ValueError, "git operation"):
            self.runtime.write_many([{"path": "new.py", "data": "YQ==", "expected": None}], expected)
        self.assertFalse((self.root / "new.py").exists())
        # This fixture owns the marker; production code never removes it.
        (self.root / ".git/index.lock").unlink()
        self.assertNotIn("index.lock", self.runtime.snapshot()["git_operations"])
        self.runtime.write_many([{"path": "new.py", "data": "YQ==", "expected": None}], expected)
        self.assertEqual((self.root / "new.py").read_bytes(), b"a")

    def test_core_excludesfile_configuration_and_content_invalidate_selection(self):
        (self.root / "new.py").write_bytes(b"new")
        self.assertIn("new.py", self.runtime.snapshot()["files"])
        excludes = self.root / ".extra-ignore"
        excludes.write_text("new.py\n", encoding="utf-8")
        self.git("config", "core.excludesFile", str(excludes))
        self.assertNotIn("new.py", self.runtime.snapshot()["files"])
        excludes.write_text("other.py\n", encoding="utf-8")
        self.assertIn("new.py", self.runtime.snapshot()["files"])

    def test_deleted_tracked_directory_is_reported_as_missing_file(self):
        (self.root / "nested").mkdir()
        path = self.root / "nested/code.py"
        path.write_bytes(b"tracked")
        self.git("add", "nested/code.py")
        self.git("commit", "-m", "nested fixture")
        self.runtime.snapshot()
        path.unlink()
        path.parent.rmdir()
        snap = self.runtime.snapshot(force=True)
        self.assertNotIn("nested/code.py", snap["files"])
        self.assertEqual(snap["excluded"]["nested/code.py"], "missing_worktree_file")

    def test_git_worktree_root_cannot_be_redirected_after_startup(self):
        self.runtime.snapshot()
        other = self.root / "other"
        other.mkdir()
        self.git("config", "core.worktree", str(other))
        with self.assertRaisesRegex(ValueError, "remain.*top level"):
            self.runtime.snapshot()


if __name__ == "__main__":
    unittest.main()
