import hashlib
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))
from worktree_bridge import agent


class EndpointBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.foreign = self.root / "foreign"
        for root, filename in ((self.repo, "own.txt"), (self.foreign, "foreign.txt")):
            root.mkdir()
            agent.git(root, "init", "-q", "-b", "main")
            agent.git(root, "config", "user.email", "test@example.com")
            agent.git(root, "config", "user.name", "Test")
            (root / filename).write_text(filename + "\n", encoding="utf-8")
            agent.git(root, "add", filename)
            agent.git(root, "commit", "-qm", "initial")

    def test_foreign_git_index_cannot_change_inventory_or_metadata(self):
        before = agent.inventory(self.repo)
        metadata = [root / ".git" / filename for root in (self.repo, self.foreign)
                    for filename in ("HEAD", "index")]
        hashes = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in metadata}
        with mock.patch.dict(os.environ, {"GIT_INDEX_FILE": str(self.foreign / ".git" / "index")}):
            self.assertEqual(agent.inventory(self.repo), before)
            self.assertTrue(agent.selected(self.repo, "own.txt"))
            self.assertFalse(agent.selected(self.repo, "foreign.txt"))
        self.assertEqual({path: hashlib.sha256(path.read_bytes()).hexdigest()
                          for path in metadata}, hashes)

    def test_repository_variables_are_removed_from_both_git_callers(self):
        injected = {key: "not-a-repository" for key in agent.GIT_LOCAL_ENV}
        injected.update({"GIT_CONFIG_KEY_0": "core.worktree", "GIT_CONFIG_VALUE_0": "elsewhere",
                         "SSH_AUTH_SOCK": "keep-auth", "GIT_CONFIG_GLOBAL": "keep-config"})
        proc = subprocess.CompletedProcess([], 0, b"ok", b"")
        with mock.patch.dict(os.environ, injected), mock.patch.object(agent.subprocess, "run", return_value=proc) as run:
            self.assertEqual(agent.git(self.repo, "rev-parse", "HEAD"), b"ok")
            self.assertTrue(agent.ignored(self.repo, "ignored.txt"))
        for call in run.call_args_list:
            env = call.kwargs["env"]
            self.assertFalse(agent.GIT_LOCAL_ENV & set(env))
            self.assertNotIn("GIT_CONFIG_KEY_0", env)
            self.assertNotIn("GIT_CONFIG_VALUE_0", env)
            self.assertEqual(env["SSH_AUTH_SOCK"], "keep-auth")
            self.assertEqual(env["GIT_CONFIG_GLOBAL"], "keep-config")
            self.assertEqual(env["GIT_OPTIONAL_LOCKS"], "0")
            self.assertEqual(env["LC_ALL"], "C")

    def test_normal_global_ignore_setting_is_preserved(self):
        ignore = self.root / "global-ignore"
        ignore.write_text("ignored.txt\n", encoding="utf-8")
        config = self.root / "global-config"
        config.write_text('[core]\n\texcludesFile = "' + ignore.as_posix() + '"\n', encoding="utf-8")
        (self.repo / "ignored.txt").write_text("private\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(config), "GIT_CONFIG_NOSYSTEM": "1"}):
            self.assertTrue(agent.ignored(self.repo, "ignored.txt"))
            self.assertNotIn("ignored.txt", agent.inventory(self.repo)["files"])

    def test_normal_root_is_accepted(self):
        self.assertEqual(agent.root_path(str(self.repo)), self.repo)

    def test_symlink_ancestor_is_rejected_without_link_privileges(self):
        parent = self.repo.parent
        original = Path.is_symlink

        def is_symlink(path):
            return path == parent or original(path)

        with mock.patch.object(Path, "is_symlink", is_symlink):
            with self.assertRaisesRegex(ValueError, "ancestor is a symlink"):
                agent.root_path(str(self.repo))

    def test_reparse_ancestor_is_rejected_without_link_privileges(self):
        parent = self.repo.parent
        original = Path.lstat
        flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024)

        def lstat(path, *args, **kwargs):
            if path == parent:
                return SimpleNamespace(st_file_attributes=flag, st_mode=stat.S_IFDIR)
            return original(path, *args, **kwargs)

        with mock.patch.object(Path, "lstat", lstat), mock.patch.object(Path, "is_symlink", return_value=False), \
                mock.patch.object(stat, "FILE_ATTRIBUTE_REPARSE_POINT", flag, create=True):
            with self.assertRaisesRegex(ValueError, "ancestor is a reparse point"):
                agent.root_path(str(self.repo))

    def test_real_symlink_ancestor_is_rejected(self):
        link = self.root / "linked"
        try:
            link.symlink_to(self.repo, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("directory symlink creation unavailable")
        child = self.repo / "child"
        child.mkdir()
        with self.assertRaisesRegex(ValueError, "ancestor is a symlink"):
            agent.root_path(str(link / "child"))

    def test_directory_scan_warning_is_fatal_even_with_zero_exit(self):
        proc = subprocess.CompletedProcess([], 0, b"own.txt\x00",
                                           b"warning: could not open directory 'blocked/': Permission denied\n")
        with mock.patch.object(agent.subprocess, "run", return_value=proc):
            with self.assertRaisesRegex(ValueError, "incomplete git scan"):
                agent.git(self.repo, "ls-files", "--others", "--exclude-standard", "-z")

    def test_unrelated_git_warning_is_not_fatal(self):
        proc = subprocess.CompletedProcess([], 0, b"own.txt\x00", b"warning: LF will be replaced by CRLF\n")
        with mock.patch.object(agent.subprocess, "run", return_value=proc):
            self.assertEqual(agent.git(self.repo, "ls-files", "--others", "-z"), b"own.txt\x00")

    def test_check_ignore_non_match_is_not_a_git_error(self):
        proc = subprocess.CompletedProcess([], 1, b"", b"")
        with mock.patch.object(agent.subprocess, "run", return_value=proc):
            self.assertFalse(agent.ignored(self.repo, "own.txt"))


if __name__ == "__main__":
    unittest.main()
