import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from worktree_bridge import agent, git_ops
from worktree_bridge.git_sync import GitCoordinator


class LocalPeer:
    def __init__(self, root):
        self.root = root

    def call(self, op, **payload):
        if op == "snapshot":
            return agent.inventory(self.root)
        return git_ops.dispatch(self.root, op, payload)


class GitSyncTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.local, self.remote = self.directory / "local", self.directory / "remote"
        self.local.mkdir()
        self.git(self.local, "init", "-q", "-b", "main")
        self.identity(self.local)
        (self.local / "a.txt").write_bytes(b"a-base\n")
        (self.local / "b.txt").write_bytes(b"b-base\n")
        self.git(self.local, "add", ".")
        self.git(self.local, "commit", "-qm", "initial")
        self.git(self.directory, "-c", "core.autocrlf=false", "clone", "-q", str(self.local), str(self.remote))
        self.identity(self.remote)
        self.peers = {"local": LocalPeer(self.local), "remote": LocalPeer(self.remote)}
        self.coordinator = GitCoordinator(self.peers, self.directory / "state")
        snapshot = self.snapshots()["local"]
        self.baseline = {"schema": 1, "id": "initial", "head": snapshot["head"],
                         "branch": "main", "files": snapshot["files"], "endpoints": {}}

    def git(self, root, *args, check=True):
        result = subprocess.run(["git", "-C", str(root), *args], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=agent.git_environment())
        if check and result.returncode:
            self.fail(result.stderr.decode("utf-8", "replace"))
        return result

    def identity(self, root):
        self.git(root, "config", "user.name", "Test")
        self.git(root, "config", "user.email", "test@example.com")
        self.git(root, "config", "core.autocrlf", "false")

    def snapshots(self):
        return {side: peer.call("snapshot") for side, peer in self.peers.items()}

    def commit(self, root, path="a.txt", content=b"a-next\n"):
        (root / path).write_bytes(content)
        self.git(root, "add", "--", path)
        self.git(root, "commit", "-qm", "next")

    def follow(self):
        return self.coordinator.reconcile(self.snapshots(), self.baseline)

    def test_both_directions_preserve_clean_target_bytes_and_file_baseline(self):
        for source_name in ("local", "remote"):
            with self.subTest(source=source_name):
                source = getattr(self, source_name)
                target = self.remote if source_name == "local" else self.local
                self.commit(source, content=("next-" + source_name).encode())
                before = (target / "a.txt").read_bytes()
                result = self.follow()
                self.assertFalse(result["blocked"], result)
                self.assertTrue(result["followed"])
                self.assertEqual(result["snapshots"]["local"]["head"], result["snapshots"]["remote"]["head"])
                self.assertEqual((target / "a.txt").read_bytes(), before)
                self.assertEqual(result["baseline"]["files"], self.baseline["files"])
                # Model the service's subsequent file sync before the reverse case.
                (target / "a.txt").write_bytes((source / "a.txt").read_bytes())
                self.baseline = result["baseline"]
                self.baseline["files"] = self.snapshots()["local"]["files"]

    def test_mirrored_dirty_in_both_directions_becomes_clean(self):
        for source_name in ("local", "remote"):
            with self.subTest(source=source_name):
                source = getattr(self, source_name)
                target = self.remote if source_name == "local" else self.local
                self.commit(source, content=(source_name + " mirror").encode())
                (target / "a.txt").write_bytes((source / "a.txt").read_bytes())
                result = self.follow()
                self.assertFalse(result["blocked"], result)
                self.assertFalse(self.git(target, "status", "--porcelain").stdout)
                self.baseline = result["baseline"]
                self.baseline["files"] = self.snapshots()["local"]["files"]

    def test_partial_mirror_preserves_every_working_byte(self):
        (self.local / "a.txt").write_bytes(b"a-next")
        (self.local / "b.txt").write_bytes(b"b-next")
        self.git(self.local, "commit", "-qam", "two changes")
        (self.remote / "a.txt").write_bytes(b"a-next")
        before = agent.inventory(self.remote)["files"]
        result = self.follow()
        self.assertFalse(result["blocked"], result)
        self.assertEqual(agent.inventory(self.remote)["files"], before)
        self.assertEqual(result["baseline"]["files"], self.baseline["files"])
        self.assertIn(b" M b.txt", self.git(self.remote, "status", "--porcelain").stdout)

    def test_independent_dirty_blocks_and_preserves_head_index_bytes(self):
        self.commit(self.local)
        (self.remote / "b.txt").write_bytes(b"independent")
        index = git_ops._index_path(self.remote).read_bytes()
        result = self.follow()
        self.assertTrue(result["blocked"], result)
        self.assertIn("independent dirty", result["reason"])
        self.assertEqual(self.git(self.remote, "rev-parse", "HEAD").stdout.decode().strip(), self.baseline["head"])
        self.assertEqual(git_ops._index_path(self.remote).read_bytes(), index)
        self.assertEqual((self.remote / "b.txt").read_bytes(), b"independent")

    def test_baseline_shared_dirty_is_preserved(self):
        for root in (self.local, self.remote):
            (root / "b.txt").write_bytes(b"common pending")
        self.baseline["files"] = self.snapshots()["local"]["files"]
        self.commit(self.local)
        result = self.follow()
        self.assertFalse(result["blocked"], result)
        self.assertEqual((self.remote / "b.txt").read_bytes(), b"common pending")

    def test_divergent_commits_block_without_transferring(self):
        self.commit(self.local, content=b"left")
        self.commit(self.remote, content=b"right")
        result = self.follow()
        self.assertTrue(result["blocked"])
        self.assertIn("divergent", result["reason"])

    def test_staged_work_on_either_side_is_never_changed(self):
        self.commit(self.local)
        for root in (self.remote, self.local):
            with self.subTest(root=root):
                (root / "b.txt").write_bytes(b"staged")
                self.git(root, "add", "b.txt")
                index = git_ops._index_path(root).read_bytes()
                result = self.follow()
                self.assertTrue(result["blocked"])
                self.assertEqual(git_ops._index_path(root).read_bytes(), index)
                self.git(root, "restore", "--staged", "b.txt")
                self.git(root, "restore", "b.txt")

    def test_git_operation_and_index_flags_block(self):
        self.commit(self.local)
        marker = self.remote / ".git" / "MERGE_HEAD"
        marker.write_text(self.baseline["head"], encoding="utf-8")
        self.assertTrue(self.follow()["blocked"])
        marker.unlink()
        self.git(self.remote, "update-index", "--assume-unchanged", "b.txt")
        self.assertTrue(self.follow()["blocked"])

    def test_intent_to_add_index_is_protected(self):
        self.commit(self.local)
        (self.remote / "intent.txt").write_bytes(b"pending intent")
        self.git(self.remote, "add", "-N", "intent.txt")
        original_index = git_ops._index_path(self.remote).read_bytes()
        result = self.follow()
        self.assertTrue(result["blocked"], result)
        self.assertEqual(git_ops._index_path(self.remote).read_bytes(), original_index)

    def test_changed_excluded_weight_is_blocked(self):
        (self.local / "model.pt").write_bytes(b"weights")
        self.git(self.local, "add", "model.pt")
        self.git(self.local, "commit", "-qm", "weight")
        result = self.follow()
        self.assertTrue(result["blocked"])
        self.assertIn("excluded", result["reason"])
        self.assertFalse((self.remote / "model.pt").exists())

    def test_excluded_history_is_rejected_before_objects_leave_source(self):
        (self.local / ".env").write_bytes(b"private secret in history")
        self.git(self.local, "add", ".env")
        self.git(self.local, "commit", "-qm", "secret fixture")
        secret = self.git(self.local, "rev-parse", "HEAD:.env").stdout.decode().strip()
        (self.local / ".env").unlink()
        (self.local / "a.txt").write_bytes(b"ordinary final tree")
        self.git(self.local, "add", "-A")
        self.git(self.local, "commit", "-qm", "remove secret")
        result = self.follow()
        self.assertTrue(result["blocked"], result)
        self.assertIn("incoming Git history", result["reason"])
        self.assertNotEqual(self.git(self.remote, "cat-file", "-e", secret, check=False).returncode, 0)
        self.assertEqual(self.snapshots()["remote"]["head"], self.baseline["head"])

    def test_secret_commit_is_rejected_before_object_transfer(self):
        (self.local / ".env").write_bytes(b"private single-commit secret")
        self.git(self.local, "add", ".env")
        self.git(self.local, "commit", "-qm", "secret fixture")
        secret = self.git(self.local, "rev-parse", "HEAD:.env").stdout.decode().strip()
        result = self.follow()
        self.assertTrue(result["blocked"], result)
        self.assertNotEqual(self.git(self.remote, "cat-file", "-e", secret, check=False).returncode, 0)

    def test_ignored_untracked_collision_is_not_overwritten(self):
        (self.remote / ".git" / "info" / "exclude").write_text("new.txt\n", encoding="utf-8")
        (self.remote / "new.txt").write_bytes(b"private ignored")
        self.commit(self.local, "new.txt", b"incoming")
        result = self.follow()
        self.assertTrue(result["blocked"])
        self.assertEqual((self.remote / "new.txt").read_bytes(), b"private ignored")

    def test_new_and_deleted_paths_follow_without_touching_files(self):
        (self.local / "new.txt").write_bytes(b"new")
        (self.local / "a.txt").unlink()
        self.git(self.local, "add", "-A")
        self.git(self.local, "commit", "-qm", "new and deleted")
        result = self.follow()
        self.assertFalse(result["blocked"], result)
        self.assertFalse((self.remote / "new.txt").exists())
        self.assertEqual((self.remote / "a.txt").read_bytes(), b"a-base\n")

    def test_checkpoint_commits_only_eligible_paths_and_propagates_tag(self):
        for root in (self.local, self.remote):
            (root / "a.txt").write_bytes(b"checkpoint bytes")
            (root / ".env").write_bytes(b"SECRET")
            (root / "model.pt").write_bytes(b"weights")
        result = self.coordinator.checkpoint("explicit checkpoint", "experiment-1", baseline=self.baseline)
        self.assertFalse(result["blocked"], result)
        for root in (self.local, self.remote):
            self.assertEqual(self.git(root, "rev-parse", "experiment-1").stdout.decode().strip(), result["checkpoint"]["head"])
            names = self.git(root, "show", "--format=", "--name-only", "HEAD").stdout
            self.assertEqual(names.strip(), b"a.txt")
            self.assertEqual((root / ".env").read_bytes(), b"SECRET")
            self.assertFalse(self.git(root, "diff", "--cached", "--name-only").stdout)

    def test_checkpoint_staged_work_rejected(self):
        for root in (self.local, self.remote):
            (root / "a.txt").write_bytes(b"new")
        self.git(self.remote, "add", "a.txt")
        index = git_ops._index_path(self.remote).read_bytes()
        with self.assertRaisesRegex(ValueError, "staged work"):
            self.coordinator.checkpoint("checkpoint", baseline=self.baseline)
        self.assertEqual(git_ops._index_path(self.remote).read_bytes(), index)

    def test_checkpoint_preserves_tracked_private_dirty_bytes_and_index_entries(self):
        (self.local / ".env").write_bytes(b"old private")
        self.git(self.local, "add", ".env")
        self.git(self.local, "commit", "-qm", "private fixture before baseline")
        self.git(self.remote, "fetch", "-q", str(self.local), "main")
        self.git(self.remote, "merge", "--ff-only", "FETCH_HEAD")
        snapshot = self.snapshots()["local"]
        self.baseline.update(head=snapshot["head"], files=snapshot["files"])
        for root in (self.local, self.remote):
            (root / "a.txt").write_bytes(b"public checkpoint")
            (root / ".env").write_bytes(b"private uncommitted bytes")
        original = self.git(self.local, "rev-parse", "HEAD:.env").stdout
        result = self.coordinator.checkpoint("public only", baseline=self.baseline)
        self.assertFalse(result["blocked"], result)
        for root in (self.local, self.remote):
            self.assertEqual((root / ".env").read_bytes(), b"private uncommitted bytes")
            self.assertEqual(self.git(root, "rev-parse", "HEAD:.env").stdout, original)
            self.assertFalse(self.git(root, "diff", "--cached", "--name-only").stdout)

    def test_tag_collision_on_either_endpoint_prevents_checkpoint(self):
        for root in (self.local, self.remote):
            (root / "a.txt").write_bytes(b"new")
        for root in (self.local, self.remote):
            with self.subTest(root=root):
                self.git(root, "tag", "existing")
                with self.assertRaisesRegex(ValueError, "tag already exists"):
                    self.coordinator.checkpoint("checkpoint", "existing", baseline=self.baseline)
                self.assertEqual(self.snapshots()["local"]["head"], self.baseline["head"])
                self.git(root, "tag", "-d", "existing")

    def test_missing_git_identity_does_not_create_commit(self):
        for root in (self.local, self.remote):
            (root / "a.txt").write_bytes(b"new")
        self.git(self.local, "config", "user.name", "")
        with self.assertRaisesRegex(ValueError, "identity"):
            self.coordinator.checkpoint("checkpoint", baseline=self.baseline)
        self.assertEqual(self.snapshots()["local"]["head"], self.baseline["head"])

    def test_failure_before_ref_keeps_original_index_and_records_journal(self):
        self.commit(self.local)
        original_index = git_ops._index_path(self.remote).read_bytes()
        real = git_ops._git
        def fail(root, *args, **kwargs):
            if root == self.remote and args[0] == "update-ref":
                raise ValueError("injected ref failure")
            return real(root, *args, **kwargs)
        with mock.patch.object(git_ops, "_git", side_effect=fail):
            result = self.follow()
        self.assertTrue(result["blocked"])
        self.assertEqual(git_ops._index_path(self.remote).read_bytes(), original_index)
        self.assertFalse(Path(str(git_ops._index_path(self.remote)) + ".lock").exists())
        journals = list((self.remote / ".git" / "worktree-bridge-journal").glob("*.json"))
        journal = json.loads(journals[0].read_text(encoding="utf-8"))
        self.assertEqual(journal["status"], "failed")
        self.assertEqual(journal["phase"], "index_prepared")
        self.assertEqual(journal["before_files"], self.baseline["files"])
        self.assertEqual(journal["before_index"], agent.digest(original_index))

    def test_failure_after_ref_keeps_lock_and_journal_for_recovery(self):
        self.commit(self.local)
        original_index = git_ops._index_path(self.remote).read_bytes()
        real = os.replace
        index = git_ops._index_path(self.remote)
        def fail(source, destination):
            if Path(destination) == index and Path(source) == Path(str(index) + ".lock"):
                raise OSError("injected index publication failure")
            return real(source, destination)
        with mock.patch.object(git_ops.os, "replace", side_effect=fail):
            result = self.follow()
        self.assertTrue(result["blocked"])
        self.assertEqual(index.read_bytes(), original_index)
        self.assertTrue(Path(str(index) + ".lock").exists())
        journals = list((self.remote / ".git" / "worktree-bridge-journal").glob("*.json"))
        journal = json.loads(journals[0].read_text(encoding="utf-8"))
        self.assertEqual(journal["phase"], "ref_updated")
        self.assertIn("retained", journal["recovery"])
        self.assertEqual((self.remote / "a.txt").read_bytes(), b"a-base\n")

    def test_ref_timeout_after_success_keeps_lock_and_observes_actual_head(self):
        self.commit(self.local)
        index = git_ops._index_path(self.remote)
        original_index = index.read_bytes()
        real = git_ops._git
        def lose_result(root, *args, **kwargs):
            output = real(root, *args, **kwargs)
            if root == self.remote and args[0] == "update-ref":
                raise subprocess.TimeoutExpired("git update-ref", 30)
            return output
        with mock.patch.object(git_ops, "_git", side_effect=lose_result):
            result = self.follow()
        self.assertTrue(result["blocked"])
        self.assertEqual(result["snapshots"]["remote"]["head"], result["snapshots"]["local"]["head"])
        self.assertEqual(index.read_bytes(), original_index)
        self.assertTrue(Path(str(index) + ".lock").exists())
        journal = json.loads(Path(result["journal"]).read_text(encoding="utf-8"))
        self.assertEqual(journal["observed_heads"]["remote"], result["snapshots"]["remote"]["head"])

    def test_rewritten_source_history_blocks_before_transfer(self):
        (self.local / "a.txt").write_bytes(b"amended root")
        self.git(self.local, "add", "a.txt")
        self.git(self.local, "commit", "--amend", "-qm", "rewritten initial")
        result = self.follow()
        self.assertTrue(result["blocked"], result)
        self.assertIn("descend", result["reason"])
        self.assertEqual(self.snapshots()["remote"]["head"], self.baseline["head"])

    def test_hash_change_before_ref_blocks_and_git_add_is_locked(self):
        self.commit(self.local)
        original_index = git_ops._index_path(self.remote).read_bytes()
        real = git_ops._git
        checked = []
        def intervene(root, *args, **kwargs):
            result = real(root, *args, **kwargs)
            if root == self.remote and args[0] == "read-tree" and kwargs.get("index") and not checked:
                (root / "b.txt").write_bytes(b"concurrent edit")
                staged = self.git(root, "add", "b.txt", check=False)
                self.assertNotEqual(staged.returncode, 0)
                self.assertIn(b"index.lock", staged.stderr)
                checked.append(True)
            return result
        with mock.patch.object(git_ops, "_git", side_effect=intervene):
            result = self.follow()
        self.assertTrue(result["blocked"])
        self.assertTrue(checked)
        self.assertEqual(git_ops._index_path(self.remote).read_bytes(), original_index)
        self.assertEqual((self.remote / "b.txt").read_bytes(), b"concurrent edit")
        self.assertEqual(self.snapshots()["remote"]["head"], self.baseline["head"])

    def test_bounded_bundle_rejects_oversized_payload(self):
        with self.assertRaisesRegex(ValueError, "limit"):
            git_ops.dispatch(self.remote, "git_bundle_import", {
                "expected_git": {"head": self.baseline["head"], "branch": "main"},
                "base_head": self.baseline["head"], "incoming_head": self.baseline["head"],
                "data": "A" * (((git_ops.MAX_BUNDLE_BYTES + 2) // 3) * 4 + 4)})


if __name__ == "__main__":
    unittest.main()
