import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))
from worktree_bridge import agent
from worktree_bridge import __main__ as bridge


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.local = self.root / "local"
        self.remote = self.root / "remote"
        self.state = self.root / "state"
        self.local.mkdir()
        self.git(self.local, "init", "-q", "-b", "main")
        self.git(self.local, "config", "user.email", "test@example.com")
        self.git(self.local, "config", "user.name", "Test")
        (self.local / "a.txt").write_text("same", encoding="utf-8")
        self.git(self.local, "add", "a.txt")
        self.git(self.local, "commit", "-qm", "initial")
        self.git(self.root, "clone", "-q", str(self.local), str(self.remote))
        config = {"local_root": str(self.local), "remote": {"kind": "local", "root": str(self.remote)},
                  "state_dir": str(self.state)}
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps(config), encoding="utf-8")
        self.plan_file = self.root / "plan.json"

    def git(self, directory, *args):
        subprocess.run(["git", "-C", str(directory), *args], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def cli(self, *args):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(SRC)
        proc = subprocess.run([sys.executable, "-m", "worktree_bridge", "--config", str(self.config), *args],
                              text=True, encoding="utf-8", stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, env=env)
        self.assertFalse(proc.stderr, proc.stderr)
        return proc.returncode, json.loads(proc.stdout)

    def baseline(self):
        code, result = self.cli("baseline")
        self.assertEqual(code, 0, result)

    def plan(self):
        return self.cli("plan", "--out", str(self.plan_file))

    def test_single_side_copy_and_backup(self):
        self.baseline()
        (self.local / "a.txt").write_text("edited", encoding="utf-8")
        code, plan = self.plan()
        self.assertEqual(code, 0, plan)
        self.assertEqual(plan["plan"]["actions"][0]["dest"], "remote")
        code, result = self.cli("apply", "--plan", str(self.plan_file))
        self.assertEqual(code, 0, result)
        self.assertEqual((self.remote / "a.txt").read_text(encoding="utf-8"), "edited")
        journal = Path(result["journal"])
        self.assertEqual((journal.parent / "0.bin").read_bytes(), b"same")
        self.assertEqual(json.loads(journal.read_text(encoding="utf-8"))["status"], "complete")

    def test_new_remote_file_copies_to_local(self):
        self.baseline()
        (self.remote / "new.txt").write_text("from remote", encoding="utf-8")
        code, plan = self.plan()
        self.assertEqual(code, 0, plan)
        self.assertEqual(plan["plan"]["actions"][0]["dest"], "local")
        code, result = self.cli("apply", "--plan", str(self.plan_file))
        self.assertEqual(code, 0, result)
        self.assertEqual((self.local / "new.txt").read_text(encoding="utf-8"), "from remote")

    def test_both_sides_edit_conflict_stops_all_writes(self):
        self.baseline()
        (self.local / "a.txt").write_text("left", encoding="utf-8")
        (self.remote / "a.txt").write_text("right", encoding="utf-8")
        code, result = self.plan()
        self.assertEqual(code, 2)
        self.assertEqual(result["plan"]["blockers"][0]["reason"], "conflict")
        code, _ = self.cli("apply", "--plan", str(self.plan_file))
        self.assertEqual(code, 2)
        self.assertEqual((self.remote / "a.txt").read_text(encoding="utf-8"), "right")

    def test_delete_is_blocked(self):
        self.baseline()
        (self.local / "a.txt").unlink()
        code, result = self.plan()
        self.assertEqual(code, 2)
        self.assertEqual(result["plan"]["blockers"][0]["reason"], "deletion_or_exclusion")

    def test_baseline_and_refresh_reject_deletions(self):
        (self.local / "a.txt").unlink()
        (self.remote / "a.txt").unlink()
        code, result = self.cli("baseline")
        self.assertEqual(code, 2)
        self.assertIn("deletion", result["error"])
        (self.local / "a.txt").write_text("same", encoding="utf-8")
        (self.remote / "a.txt").write_text("same", encoding="utf-8")
        self.baseline()
        (self.local / "a.txt").unlink()
        (self.remote / "a.txt").unlink()
        code, result = self.cli("baseline", "--refresh")
        self.assertEqual(code, 2)
        self.assertIn("deletion", result["error"])

    def test_stale_plan_is_rejected(self):
        self.baseline()
        (self.local / "a.txt").write_text("first", encoding="utf-8")
        self.assertEqual(self.plan()[0], 0)
        (self.local / "a.txt").write_text("second", encoding="utf-8")
        code, result = self.cli("apply", "--plan", str(self.plan_file))
        self.assertEqual(code, 2)
        self.assertIn("stale", result["error"])
        self.assertEqual((self.remote / "a.txt").read_text(encoding="utf-8"), "same")

    def test_plan_output_cannot_replace_project_config_or_baseline(self):
        self.baseline()
        original = (self.local / "a.txt").read_bytes()
        for output in (self.local / "a.txt", self.remote / "plan.json",
                       self.config, self.state / "baseline.json"):
            code, result = self.cli("plan", "--out", str(output))
            self.assertEqual(code, 2)
            self.assertIn("plan output", result["error"])
        self.assertEqual((self.local / "a.txt").read_bytes(), original)

    def test_partial_failure_keeps_baseline_and_journal(self):
        self.baseline()
        original_baseline = (self.state / "baseline.json").read_bytes()
        (self.local / "a.txt").write_text("first", encoding="utf-8")
        (self.local / "b.txt").write_text("second", encoding="utf-8")
        local, remote, state = bridge.configuration(self.config)
        identity = bridge.endpoint_identity(local, remote)
        plan = bridge.make_plan(bridge.load_baseline(state, identity), bridge.snapshots(local, remote))
        original_call = remote.call

        def fail_second(op, **kwargs):
            if op == "write" and kwargs["path"] == "b.txt":
                raise RuntimeError("simulated interruption")
            return original_call(op, **kwargs)

        with mock.patch.object(remote, "call", side_effect=fail_second):
            with self.assertRaises(RuntimeError):
                bridge.execute(plan, state, {"local": local, "remote": remote})
        self.assertEqual((self.remote / "a.txt").read_text(encoding="utf-8"), "first")
        self.assertFalse((self.remote / "b.txt").exists())
        self.assertEqual((self.state / "baseline.json").read_bytes(), original_baseline)
        journals = list((self.state / "journal").glob("*/journal.json"))
        self.assertEqual(len(journals), 1)
        record = json.loads(journals[0].read_text(encoding="utf-8"))
        self.assertEqual(record["status"], "interrupted")
        self.assertEqual(record["completed"], [0])

    def test_git_branch_mismatch_blocks_plan(self):
        self.baseline()
        self.git(self.remote, "checkout", "-qb", "different")
        code, result = self.plan()
        self.assertEqual(code, 2)
        self.assertEqual(result["plan"]["blockers"][0]["reason"], "git_mismatch")

    def test_endpoint_identity_and_overlapping_roots_rejected(self):
        self.baseline()
        changed = json.loads(self.config.read_text(encoding="utf-8"))
        changed["remote"]["root"] = str(self.local)
        self.config.write_text(json.dumps(changed), encoding="utf-8")
        code, result = self.cli("plan")
        self.assertEqual(code, 2)
        self.assertIn("separate worktrees", result["error"])
        another = self.root / "another"
        self.git(self.root, "clone", "-q", str(self.local), str(another))
        changed["remote"]["root"] = str(another)
        self.config.write_text(json.dumps(changed), encoding="utf-8")
        code, result = self.cli("plan")
        self.assertEqual(code, 2)
        self.assertIn("different endpoints", result["error"])

    def test_status_reports_document_hashes_and_differences(self):
        (self.local / "AGENTS.md").write_text("context", encoding="utf-8")
        (self.remote / "AGENTS.md").write_text("context", encoding="utf-8")
        (self.remote / "a.txt").write_text("edited", encoding="utf-8")
        code, result = self.cli("status")
        self.assertEqual(code, 0, result)
        self.assertEqual(result["differences"]["different"], ["a.txt"])
        self.assertEqual(result["endpoints"]["local"]["docs"]["AGENTS.md"],
                         agent.digest(b"context"))

    def test_read_and_write_recheck_git_and_secrets(self):
        snapshot = agent.inventory(self.remote)
        (self.remote / ".env").write_text("private", encoding="utf-8")
        with self.assertRaises(ValueError):
            agent.dispatch({"root": str(self.remote), "op": "read", "path": ".env",
                            "expect_head": snapshot["head"], "expect_branch": snapshot["branch"]})
        with self.assertRaises(ValueError):
            agent.dispatch({"root": str(self.remote), "op": "write", "path": ".env",
                            "data": "bmV3", "expected": agent.digest(b"private"),
                            "expect_head": snapshot["head"], "expect_branch": snapshot["branch"]})
        self.git(self.remote, "checkout", "-qb", "changed")
        with self.assertRaisesRegex(ValueError, "git branch/HEAD changed"):
            agent.dispatch({"root": str(self.remote), "op": "write", "path": "a.txt",
                            "data": "bmV3", "expected": snapshot["files"]["a.txt"],
                            "expect_head": snapshot["head"], "expect_branch": snapshot["branch"]})
        self.assertEqual((self.remote / "a.txt").read_text(encoding="utf-8"), "same")

    def test_inventory_read_error_is_fatal(self):
        with mock.patch.object(agent, "read_bytes", side_effect=PermissionError("denied")):
            with self.assertRaisesRegex(OSError, "cannot read a.txt: denied"):
                agent.inventory(self.local)

    def test_cross_platform_case_collision_blocks_plan(self):
        base = {"id": "base", "head": "abc", "branch": "main", "files": {}, "endpoints": {}}
        common = {"head": "abc", "branch": "main", "dirty": True, "docs": {},
                  "excluded": {}, "unmerged": [], "git_operations": []}
        snap = {"local": {**common, "files": {"AGENTS.md": "a"}},
                "remote": {**common, "files": {"agents.md": "b"}}}
        plan = bridge.make_plan(base, snap)
        self.assertIn("case_collision", [item["reason"] for item in plan["blockers"]])

    def test_initial_divergence_rejected(self):
        (self.remote / "a.txt").write_text("diverged", encoding="utf-8")
        code, result = self.cli("baseline")
        self.assertEqual(code, 2)
        self.assertIn("initial divergence", result["error"])
        self.assertFalse((self.state / "baseline.json").exists())

    def test_changed_baseline_head_blocks_plan_until_explicit_refresh(self):
        self.baseline()
        (self.local / "a.txt").write_text("committed", encoding="utf-8")
        self.git(self.local, "add", "a.txt")
        self.git(self.local, "commit", "-qm", "advance")
        self.git(self.remote, "pull", "-q", "--ff-only", str(self.local), "main")
        (self.local / "a.txt").write_text("same", encoding="utf-8")
        code, plan = self.plan()
        self.assertEqual(code, 2)
        self.assertIn("baseline_head_changed", [item["reason"] for item in plan["plan"]["blockers"]])
        self.assertEqual(self.cli("apply", "--plan", str(self.plan_file))[0], 2)
        self.assertEqual((self.local / "a.txt").read_text(encoding="utf-8"), "same")
        (self.local / "a.txt").write_text("committed", encoding="utf-8")
        code, result = self.cli("baseline", "--refresh")
        self.assertEqual(code, 0, result)
        self.assertTrue(Path(result["previous_baseline"]).exists())

    def test_ignored_new_destination_blocks_before_write(self):
        self.baseline()
        (self.local / "scratch").mkdir()
        (self.local / "scratch" / "x.txt").write_text("new", encoding="utf-8")
        (self.remote / ".git" / "info" / "exclude").write_text("scratch/\n", encoding="utf-8")
        code, plan = self.plan()
        self.assertEqual(code, 2)
        self.assertIn("destination_unselected", [item["reason"] for item in plan["plan"]["blockers"]])
        self.assertEqual(self.cli("apply", "--plan", str(self.plan_file))[0], 2)
        self.assertFalse((self.remote / "scratch" / "x.txt").exists())

    def test_direct_read_of_ignored_file_rejected(self):
        (self.local / ".git" / "info" / "exclude").write_text("token.txt\n", encoding="utf-8")
        (self.local / "token.txt").write_text("secret", encoding="utf-8")
        snapshot = agent.inventory(self.local)
        with self.assertRaisesRegex(ValueError, "outside Git selection"):
            agent.dispatch({"root": str(self.local), "op": "read", "path": "token.txt",
                            "expect_head": snapshot["head"], "expect_branch": snapshot["branch"]})

    def test_unmerged_index_is_a_plan_blocker(self):
        base = {"id": "base", "head": "abc", "branch": "main", "files": {}, "endpoints": {}}
        common = {"head": "abc", "branch": "main", "dirty": True, "docs": {},
                  "excluded": {}, "unmerged": [], "git_operations": []}
        snap = {"local": {**common, "files": {}, "unmerged": ["a.txt"]},
                "remote": {**common, "files": {}}}
        plan = bridge.make_plan(base, snap)
        self.assertIn("unmerged_index", [item["reason"] for item in plan["blockers"]])

    def test_git_operation_marker_blocks_plan(self):
        self.baseline()
        head = subprocess.check_output(["git", "-C", str(self.local), "rev-parse", "HEAD"], text=True)
        (self.local / ".git" / "MERGE_HEAD").write_text(head, encoding="utf-8")
        code, result = self.plan()
        self.assertEqual(code, 2)
        self.assertIn("git_operation_in_progress", [item["reason"] for item in result["plan"]["blockers"]])

    def test_git_and_secrets_excluded(self):
        (self.local / ".env").write_text("SECRET", encoding="utf-8")
        (self.remote / ".env").write_text("OTHER", encoding="utf-8")
        code, result = self.cli("status")
        self.assertEqual(code, 0, result)
        self.assertEqual(result["endpoints"]["local"]["file_count"], 1)
        self.assertEqual(result["endpoints"]["remote"]["file_count"], 1)
        self.assertEqual(result["endpoints"]["local"]["excluded"][".env"], "excluded_by_policy")
        self.baseline()

    def test_tracked_dataset_loader_is_selected_but_data_file_is_not(self):
        folder = self.local / "datasets"
        folder.mkdir()
        (folder / "build.py").write_text("VALUE = 1\n", encoding="utf-8")
        (folder / "samples.bin").write_bytes(b"data")
        self.git(self.local, "add", "datasets/build.py", "datasets/samples.bin")
        self.git(self.local, "commit", "-qm", "dataset fixture")
        self.git(self.remote, "pull", "-q", "--ff-only", str(self.local), "main")
        code, result = self.cli("status")
        self.assertEqual(code, 0, result)
        self.assertEqual(result["endpoints"]["local"]["file_count"], 2)
        self.assertEqual(result["endpoints"]["local"]["excluded"]["datasets/samples.bin"],
                         "excluded_by_policy")
        self.baseline()
        (folder / "build.py").write_text("VALUE = 2\n", encoding="utf-8")
        code, plan = self.plan()
        self.assertEqual(code, 0, plan)
        self.assertEqual(plan["plan"]["actions"][0]["path"], "datasets/build.py")

    def test_path_escape_and_symlink_rejected(self):
        with self.assertRaises(ValueError):
            agent.safe_path(self.local, "../outside")
        with self.assertRaises(ValueError):
            agent.safe_path(self.local, ".git/config")
        for path in ("CON.txt", "bad.", "bad ", "dir/AUX.json"):
            with self.assertRaises(ValueError):
                agent.safe_path(self.local, path)
        link = self.local / "link.txt"
        try:
            link.symlink_to(self.local / "a.txt")
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation unavailable")
        with self.assertRaises(ValueError):
            agent.read_bytes(self.local, "link.txt")


if __name__ == "__main__":
    unittest.main()
