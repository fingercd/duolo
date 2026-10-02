from concurrent.futures import ThreadPoolExecutor
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
from worktree_bridge import registry


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.app = self.base / "private-app"
        env = mock.patch.dict(os.environ, {
            "LOCALAPPDATA": str(self.app), "XDG_STATE_HOME": str(self.app),
        })
        env.start()
        self.addCleanup(env.stop)
        self.local = self.repo("local")
        self.remote = {"kind": "local", "root": str(self.base / "peer")}

    def git(self, root, *args):
        return subprocess.run(["git", "-C", str(root), *args], env=registry.git_environment(),
                              capture_output=True, text=True, encoding="utf-8", check=True).stdout.strip()

    def repo(self, name):
        root = self.base / name
        root.mkdir(parents=True)
        self.git(root, "init", "-q", "-b", "main")
        self.git(root, "config", "user.name", "Registry Test")
        self.git(root, "config", "user.email", "registry@example.com")
        return root

    def init(self, root=None, **kwargs):
        return registry.init_project(root or self.local, **({"remote": self.remote} | kwargs))

    def test_register_existing_repo_preserves_original_bytes_and_status(self):
        payload = self.local / "experiment.txt"
        payload.write_bytes(b"original\x00bytes\r\n")
        git_config = (self.local / ".git" / "config").read_bytes()
        status = self.git(self.local, "status", "--porcelain")
        result = self.init(name="experiment")
        self.assertTrue(result["registered"])
        config_path = Path(result["config_path"])
        self.assertEqual(config_path, self.local / ".git" / "worktree-bridge" / "config.json")
        config = json.loads(config_path.read_text(encoding="utf-8"))
        self.assertEqual(config["local_root"], str(self.local))
        self.assertEqual(config["remote"], self.remote)
        self.assertNotIn(self.local, Path(config["state_dir"]).parents)
        self.assertEqual(payload.read_bytes(), b"original\x00bytes\r\n")
        self.assertEqual((self.local / ".git" / "config").read_bytes(), git_config)
        self.assertEqual(self.git(self.local, "status", "--porcelain"), status)
        head = subprocess.run(["git", "-C", str(self.local), "rev-parse", "--verify", "HEAD"],
                              capture_output=True, env=registry.git_environment())
        self.assertNotEqual(head.returncode, 0, "registration must not create an initial commit")
        self.assertEqual(len(registry.list_projects()), 1)
        self.assertEqual(registry.list_projects()[0]["name"], "experiment")
        self.assertNotIn("remote", registry.list_projects()[0])

    def test_subdirectory_discovery_and_list_never_run_git(self):
        result = self.init()
        child = self.local / "a" / "b"
        child.mkdir(parents=True)
        with mock.patch.object(registry.subprocess, "run", side_effect=AssertionError("no subprocess in hot path")):
            self.assertEqual(registry.find_config(child), Path(result["config_path"]))
            self.assertEqual(len(registry.list_projects()), 1)

    def test_linked_worktree_gets_its_own_private_config_and_identity(self):
        (self.local / "a.txt").write_text("initial", encoding="utf-8")
        self.git(self.local, "add", "a.txt")
        self.git(self.local, "commit", "-qm", "initial")
        linked = self.base / "linked"
        self.git(self.local, "worktree", "add", "-q", "-b", "linked", str(linked))
        first = self.init()
        second = self.init(linked)
        gitdir = Path(self.git(linked, "rev-parse", "--absolute-git-dir"))
        self.assertEqual(Path(second["config_path"]), gitdir / "worktree-bridge" / "config.json")
        self.assertNotEqual(first["project_id"], second["project_id"])
        self.assertNotEqual(first["state_dir"], second["state_dir"])
        with mock.patch.object(registry.subprocess, "run", side_effect=AssertionError("hot path")):
            self.assertEqual(registry.find_config(linked), Path(second["config_path"]))
        self.assertEqual(len(registry.list_projects()), 2)

    def test_nearest_nested_repository_is_a_boundary(self):
        outer = self.init()
        nested = self.repo("local/nested")
        with self.assertRaisesRegex(RuntimeError, "not registered"):
            registry.find_config(nested)
        registered = self.init(nested)
        self.assertEqual(registry.find_config(nested), Path(registered["config_path"]))
        self.assertNotEqual(registered["config_path"], outer["config_path"])

    def test_invalid_nearest_git_marker_does_not_fall_back_to_parent(self):
        self.init()
        nested = self.local / "nested"
        nested.mkdir()
        (nested / ".git").write_text("not a gitdir", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "Invalid Git"):
            registry.find_config(nested)

    def test_duplicate_binding_is_idempotent_and_different_binding_refused(self):
        first = self.init(name="stable")
        path = Path(first["config_path"])
        original = path.read_bytes()
        index = (registry._registry_root() / "registry.json").read_bytes()
        again = self.init()
        self.assertFalse(again["registered"])
        self.assertEqual(again["project_id"], first["project_id"])
        self.assertFalse(registry.init_project(self.local)["registered"])
        with self.assertRaisesRegex(RuntimeError, "different remote"):
            self.init(remote={"kind": "local", "root": str(self.base / "other")})
        with self.assertRaisesRegex(RuntimeError, "different name"):
            self.init(name="changed")
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual((registry._registry_root() / "registry.json").read_bytes(), index)

    @unittest.skipUnless(os.name == "nt", "Windows path case aliases")
    def test_windows_case_alias_registration_preserves_configuration_and_index(self):
        first = self.init()
        path = Path(first["config_path"])
        original_config = path.read_bytes()
        index_path = registry._registry_root() / "registry.json"
        original_index = index_path.read_bytes()
        alias = Path(str(self.local).swapcase())
        self.assertTrue(alias.is_dir())
        same_remote = {"kind": "local", "root": self.remote["root"].swapcase()}
        repeated = registry.init_project(alias, remote=same_remote)
        self.assertFalse(repeated["registered"])
        self.assertEqual(repeated["project_id"], first["project_id"])
        self.assertEqual(Path(repeated["config_path"]), path)
        with mock.patch.object(registry.subprocess, "run", side_effect=AssertionError("no Git hot path")):
            self.assertEqual(registry.find_config(alias), path)
        self.assertEqual(path.read_bytes(), original_config)
        self.assertEqual(index_path.read_bytes(), original_index)

    def test_migration_preserves_all_fields_state_and_unnamed_fingerprint(self):
        config = {"local_root": str(self.local), "remote": self.remote,
                  "state_dir": str(self.base / "existing-state"),
                  "service": {"auto_sync": False}, "custom": {"seed": 17}}
        source = self.base / "old-config.json"
        source.write_text(json.dumps(config, indent=3), encoding="utf-8")
        original = source.read_bytes()
        result = registry.init_project(self.local, from_config=source)
        migrated = json.loads(Path(result["config_path"]).read_text(encoding="utf-8"))
        self.assertEqual(migrated, config)
        self.assertNotIn("name", migrated)
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(result["state_dir"], config["state_dir"])
        self.assertFalse(registry.init_project(self.local, from_config=source)["registered"])
        self.assertFalse(self.init()["registered"])

    def test_migration_explicit_name_is_the_only_field_added(self):
        config = {"local_root": str(self.local), "remote": self.remote,
                  "state_dir": str(self.base / "existing-state")}
        source = self.base / "old.json"
        source.write_text(json.dumps(config), encoding="utf-8")
        result = registry.init_project(self.local, name="explicit", from_config=source)
        self.assertEqual(json.loads(Path(result["config_path"]).read_text()), config | {"name": "explicit"})

    def test_migration_from_wrong_worktree_or_internal_state_is_refused(self):
        source = self.base / "old.json"
        valid = {"local_root": str(self.local), "remote": self.remote,
                 "state_dir": str(self.base / "old-state")}
        variants = [valid | {"local_root": str(self.base / "other")},
                    valid | {"state_dir": str(self.local / ".git" / "state")},
                    valid | {"state_dir": str(Path(self.remote["root"]) / "state")},
                    valid | {"state_dir": "relative-state"}]
        for config in variants:
            with self.subTest(config=config):
                source.write_text(json.dumps(config), encoding="utf-8")
                with self.assertRaises(RuntimeError):
                    registry.init_project(self.local, from_config=source)
        self.assertFalse((self.local / ".git" / "worktree-bridge").exists())
        self.assertEqual(registry.list_projects(), [])

    def test_ssh_registration_validates_without_connecting(self):
        remote = {"kind": "ssh", "host": "research-alias", "root": "/srv/project", "port": 2222}
        with mock.patch.object(registry.subprocess, "run", wraps=subprocess.run) as calls:
            result = self.init(remote=remote)
        self.assertTrue(result["ok"])
        for call in calls.call_args_list:
            self.assertEqual(call.args[0][0], "git")
            self.assertEqual(call.args[0][3], "rev-parse")
        self.assertNotIn("host", registry.list_projects()[0])

    def test_remote_input_boundaries(self):
        variants = [{"kind": "ssh", "host": "-oProxyCommand=x", "root": "/srv/p"},
                    {"kind": "ssh", "host": "host name", "root": "/srv/p"},
                    {"kind": "ssh", "host": "host", "root": "relative"},
                    {"kind": "ssh", "host": "host", "root": "/srv/p", "port": True},
                    {"kind": "ssh", "host": "host", "root": "/srv/p", "port": 0},
                    {"kind": "local", "root": str(self.local)},
                    {"kind": "local", "root": str(self.local / "nested")},
                    {"kind": "local", "root": "relative"}]
        for remote in variants:
            with self.subTest(remote=remote):
                with self.assertRaises(RuntimeError):
                    self.init(remote=remote)
        self.assertEqual(registry.list_projects(), [])

    def test_empty_registry_and_friendly_unregistered_errors(self):
        self.assertEqual(registry.list_projects(), [])
        with self.assertRaisesRegex(RuntimeError, "not registered"):
            registry.find_config(self.local)
        with self.assertRaisesRegex(RuntimeError, "required"):
            registry.init_project(self.local)
        ordinary = self.base / "ordinary"
        ordinary.mkdir()
        with self.assertRaisesRegex(RuntimeError, "existing Git"):
            registry.init_project(ordinary, remote=self.remote)
        self.assertFalse((ordinary / ".git").exists())

    def test_concurrent_registration_keeps_every_project(self):
        roots = [self.local, self.repo("second"), self.repo("third"), self.repo("fourth")]
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda root: self.init(root), roots))
        self.assertEqual(len(registry.list_projects()), 4)
        self.assertEqual({item["project_id"] for item in results},
                         {item["project_id"] for item in registry.list_projects()})
        self.assertFalse((registry._registry_root() / "registry.lock").exists())

    def test_concurrent_same_worktree_creates_one_binding(self):
        with ThreadPoolExecutor(max_workers=3) as executor:
            results = list(executor.map(lambda _: self.init(), range(3)))
        self.assertEqual(sum(result["registered"] for result in results), 1)
        self.assertEqual(len({result["project_id"] for result in results}), 1)
        self.assertEqual(len(registry.list_projects()), 1)

    def test_equivalent_local_root_and_migrated_state_remain_stable(self):
        config = {"local_root": str(self.local),
                  "remote": {"kind": "local", "root": str(self.base / "peer") + os.sep + "."},
                  "state_dir": str(self.base / "old-state")}
        source = self.base / "old.json"
        source.write_text(json.dumps(config), encoding="utf-8")
        migrated = registry.init_project(self.local, from_config=source)
        path = Path(migrated["config_path"])
        original = path.read_bytes()
        result = self.init()
        self.assertFalse(result["registered"])
        self.assertEqual(result["state_dir"], config["state_dir"])
        self.assertEqual(path.read_bytes(), original)

    def test_default_registry_and_state_cannot_be_inside_worktree(self):
        inside = self.local / "app-data"
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(inside), "XDG_STATE_HOME": str(inside)}):
            with self.assertRaisesRegex(RuntimeError, "outside both worktrees"):
                self.init()
        self.assertFalse(inside.exists())
        self.assertFalse((self.local / ".git" / "worktree-bridge").exists())

    def test_existing_malformed_configuration_is_preserved(self):
        path = self.local / ".git" / "worktree-bridge" / "config.json"
        path.parent.mkdir()
        path.write_bytes(b'{"keep": "original"}')
        with self.assertRaisesRegex(RuntimeError, "local_root"):
            registry.init_project(self.local)
        self.assertEqual(path.read_bytes(), b'{"keep": "original"}')
        self.assertEqual(registry.list_projects(), [])

    def test_git_must_confirm_metadata_before_any_registration_write(self):
        marker = self.local / ".git"
        marker.rename(self.base / "original-gitdir")
        fake = self.base / "empty-gitdir"
        fake.mkdir()
        marker.write_text("gitdir: " + str(fake), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "Git did not confirm"):
            self.init()
        self.assertEqual(list(fake.iterdir()), [])
        self.assertEqual(registry.list_projects(), [])

    def test_existing_registry_lock_is_not_removed_or_bypassed(self):
        base = registry._registry_root()
        base.mkdir(parents=True)
        lock = base / "registry.lock"
        lock.write_text("other-owner", encoding="utf-8")
        with mock.patch.object(registry.time, "monotonic", side_effect=[0, 6]):
            with self.assertRaisesRegex(RuntimeError, "Registration is locked"):
                self.init()
        self.assertEqual(lock.read_text(), "other-owner")
        self.assertFalse((self.local / ".git" / "worktree-bridge").exists())

    def test_invalid_registry_is_not_overwritten(self):
        base = registry._registry_root()
        base.mkdir(parents=True)
        index = base / "registry.json"
        index.write_text('{"schema":99,"original":"preserve"}', encoding="utf-8")
        original = index.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "refusing to overwrite"):
            self.init()
        self.assertEqual(index.read_bytes(), original)
        self.assertFalse((self.local / ".git" / "worktree-bridge").exists())

    def test_config_with_other_local_root_cannot_be_discovered(self):
        result = self.init()
        path = Path(result["config_path"])
        config = json.loads(path.read_text())
        config["local_root"] = str(self.base / "different")
        path.write_text(json.dumps(config))
        with self.assertRaisesRegex(RuntimeError, "local_root"):
            registry.find_config(self.local)

    def test_relative_gitdir_file_is_discovered(self):
        result = self.init()
        marker = self.local / ".git"
        moved = self.base / "moved-gitdir"
        marker.rename(moved)
        marker.write_text("gitdir: ../moved-gitdir\n", encoding="utf-8")
        self.assertEqual(registry.find_config(self.local), moved / "worktree-bridge" / "config.json")

    def test_metadata_symlink_and_config_parent_symlink_are_refused(self):
        target = self.base / "target"
        target.mkdir()
        probe = self.base / "probe"
        try:
            probe.symlink_to(target, target_is_directory=True)
        except OSError as exc:
            self.skipTest("This host cannot create symlinks: " + str(exc))
        probe.unlink()
        marker = self.local / ".git"
        metadata = self.base / "metadata"
        marker.rename(metadata)
        marker.symlink_to(metadata, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, "symlink/reparse"):
            registry.find_config(self.local)
        with self.assertRaisesRegex(RuntimeError, "symlink/reparse"):
            self.init()
        marker.unlink()
        marker.write_text("gitdir: " + str(probe) + "\n", encoding="utf-8")
        probe.symlink_to(metadata, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, "symlink/reparse"):
            registry.find_config(self.local)

    def test_windows_reparse_metadata_is_refused(self):
        if os.name != "nt":
            self.skipTest("Windows junction test")
        folder = self.local / ".git" / "worktree-bridge"
        target = self.base / "junction-target"
        target.mkdir()
        command = ['cmd', '/c', 'mklink', '/J', str(folder), str(target)]
        result = subprocess.run(command, capture_output=True)
        if result.returncode:
            self.skipTest("Cannot create test junction")
        try:
            with self.assertRaisesRegex(RuntimeError, "reparse"):
                self.init()
            with self.assertRaisesRegex(RuntimeError, "reparse"):
                registry.find_config(self.local)
        finally:
            folder.rmdir()


if __name__ == "__main__":
    unittest.main()
