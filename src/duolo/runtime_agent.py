"""Persistent, standard-library endpoint runtime (also shipped to Python 3.9 SSH peers)."""
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time

from . import agent
from .watching import Watcher

MARKERS = ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "REBASE_HEAD",
           "rebase-merge", "rebase-apply", "sequencer", "BISECT_LOG", "index.lock")


def signature(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    return (info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns,
            info.st_ino, getattr(info, "st_file_attributes", 0), info.st_dev)


class Runtime:
    def __init__(self, root, reconcile_interval=30):
        self._root_value = str(root)
        self.root = agent.root_path(root)
        top = agent.git(self.root, "rev-parse", "--show-toplevel").decode("utf-8", "surrogateescape").strip()
        if Path(top).resolve() != self.root.resolve():
            raise ValueError("root must be the git worktree top level")
        self.git_dir = Path(agent.git(self.root, "rev-parse", "--absolute-git-dir").decode("utf-8", "replace").strip())
        common = agent.git(self.root, "rev-parse", "--git-common-dir").decode("utf-8", "replace").strip()
        self.common_dir = (self.root / common).resolve()
        self._refresh_exclude_config()
        extra = [p for p in (self.git_dir, self.common_dir) if p != self.root and self.root not in p.parents]
        self.watcher = Watcher(self.root, extra_roots=list(dict.fromkeys(extra)))
        self.reconcile_interval = reconcile_interval
        self._last_reconcile = 0
        self._cache = {}
        self._paths = set()
        self._tracked = set()
        self._submodules = set()
        self._listed_excluded = {}
        self._directories = {self.root}
        self._directory_sig = {}
        self._ignore_sig = {}
        self._meta_sig = None
        self._git = None
        self._invalidated = True
        self._lease_fd = None
        self._lease_owner = None

    def _refresh_exclude_config(self):
        proc = agent.git_process(self.root, "config", "--path", "--get", "core.excludesfile")
        if proc.returncode not in (0, 1):
            raise ValueError("cannot read git excludes configuration")
        self.global_exclude = Path(os.path.expanduser(proc.stdout.decode("utf-8", "replace").strip())) if proc.stdout.strip() else Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "git" / "ignore"
        if not self.global_exclude.is_absolute():
            self.global_exclude = self.root / self.global_exclude

    def close(self):
        self.watcher.close()
        if self._lease_fd is not None:
            os.close(self._lease_fd)
            self._lease_fd = None

    def claim_controller(self, instance_id):
        if not isinstance(instance_id, str) or not instance_id:
            raise ValueError("controller instance_id is required")
        if self._lease_fd is not None:
            if instance_id != self._lease_owner:
                raise ValueError("peer already belongs to another controller instance")
            return {"claimed": True, "instance_id": instance_id}
        path = self.git_dir / "worktree-bridge-writer.lock"
        existing = signature(path)
        if existing is not None and (not stat.S_ISREG(existing[0])
                                     or existing[5] & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
            raise ValueError("controller lease path must be a regular file")
        fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            if os.name == "nt":
                import msvcrt
                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"0")
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            raise ValueError("controller lease held by another peer; close the active controller first") from exc
        self._lease_fd = fd
        self._lease_owner = instance_id
        return {"claimed": True, "instance_id": instance_id}

    def _ensure_writer(self):
        if self._lease_fd is None:
            self.claim_controller("peer:" + str(os.getpid()))

    def invalidate(self):
        self._invalidated = True

    def _metadata(self):
        paths = {self.root / ".git", self.git_dir / "HEAD", self.git_dir / "index",
                 self.git_dir / "commondir", self.git_dir / "config.worktree",
                 self.common_dir / "packed-refs", self.common_dir / "config",
                 self.common_dir / "shallow", self.common_dir / "info" / "exclude",
                 Path.home() / ".gitconfig",
                 Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "git" / "config"}
        paths.update(self.git_dir / marker for marker in MARKERS)
        # refs can move without changing HEAD (commit/reset/update-ref).
        for base in {self.common_dir / "refs", self.git_dir / "refs"}:
            if signature(base) is not None:
                def failure(exc):
                    raise exc
                for directory, dirs, files in os.walk(base, followlinks=False, onerror=failure):
                    paths.add(Path(directory))
                    paths.update(Path(directory) / name for name in files)
        return {str(path): signature(path) for path in paths}

    def _validate_git_location(self, meta):
        if self._meta_sig is None:
            return
        config_paths = (self.common_dir / "config", self.git_dir / "config.worktree",
                        Path.home() / ".gitconfig",
                        Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "git" / "config")
        locator_paths = (self.root / ".git", self.git_dir / "commondir")
        changed_config = any(meta.get(str(path)) != self._meta_sig.get(str(path)) for path in config_paths)
        changed_locator = any(meta.get(str(path)) != self._meta_sig.get(str(path))
                              for path in locator_paths if path.is_file())
        if not (changed_config or changed_locator):
            return
        top = agent.git(self.root, "rev-parse", "--show-toplevel").decode("utf-8", "surrogateescape").strip()
        if Path(top).resolve() != self.root.resolve():
            raise ValueError("root must remain the git worktree top level")
        if changed_locator:
            git_dir = Path(agent.git(self.root, "rev-parse", "--absolute-git-dir").decode("utf-8", "replace").strip())
            common = agent.git(self.root, "rev-parse", "--git-common-dir").decode("utf-8", "replace").strip()
            if git_dir != self.git_dir or (self.root / common).resolve() != self.common_dir:
                raise ValueError("Git directory identity changed; recreate the peer")

    def _ignores(self):
        paths = {directory / ".gitignore" for directory in self._directories}
        paths.update((self.common_dir / "info" / "exclude", self.global_exclude))
        return {str(path): signature(path) for path in paths}

    def _refresh_git(self, selection=True, metadata_changed=True):
        if metadata_changed:
            head = agent.git(self.root, "rev-parse", "HEAD").decode("ascii").strip()
            branch = agent.git(self.root, "branch", "--show-current").decode("utf-8", "replace").strip()
        else:
            head, branch = self._git["head"], self._git["branch"]
        dirty = bool(agent.git(self.root, "status", "--porcelain", "-z", "--untracked-files=normal"))
        operations = [marker for marker in MARKERS if signature(self.git_dir / marker) is not None]
        if selection:
            if metadata_changed:
                staged = agent.git(self.root, "ls-files", "--stage", "-z")
                self._tracked, self._submodules, unmerged = set(), set(), set()
                for entry in staged.split(b"\0"):
                    if not entry:
                        continue
                    metadata, raw = entry.split(b"\t", 1)
                    try:
                        rel = raw.decode("utf-8")
                    except UnicodeError:
                        continue
                    self._tracked.add(rel)
                    if metadata.startswith(b"160000 "):
                        self._submodules.add(rel)
                    if metadata.rsplit(b" ", 1)[1] != b"0":
                        unmerged.add(rel)
            else:
                unmerged = self._git["unmerged"]
            listed = agent.git(self.root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
            self._paths, self._listed_excluded = set(), {}
            directories = {self.root}
            for raw in set(listed.split(b"\0")) - {b""}:
                try:
                    rel = raw.decode("utf-8")
                except UnicodeError:
                    self._listed_excluded["<bytes:" + raw.hex() + ">"] = "invalid_utf8_path"
                    continue
                self._paths.add(rel)
                try:
                    parts = agent.path_parts(rel)
                    if not agent.allowed(rel, rel in self._tracked):
                        continue
                    parent = self.root
                    for part in parts[:-1]:
                        parent = parent / part
                        if parent.is_symlink() or not parent.is_dir() or (parent / ".git").exists():
                            break
                        directories.add(parent)
                except ValueError:
                    continue
            self._directories = directories
        else:
            unmerged = self._git["unmerged"]
        self._git = {"head": head, "branch": branch, "dirty": dirty,
                     "git_operations": operations, "unmerged": sorted(unmerged)}

    def _snapshot_target(self, rel, parents):
        # Validate each ancestor once per scan and each target with one lstat.
        # Calling the one-shot safe_path for every cached file repeats several
        # identical Windows filesystem calls and dominates warm snapshots.
        parts = agent.path_parts(rel)
        parent = self.root
        for part in parts[:-1]:
            parent = parent / part
            key = str(parent)
            if key not in parents:
                info = signature(parent)
                if info is None:
                    # Removing a tracked file and its now-empty directory is a
                    # deletion, not an unsafe/excluded parent substitution.
                    return self.root.joinpath(*parts), None
                if not stat.S_ISDIR(info[0]) or info[5] & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
                    raise ValueError("linked or invalid parent: " + rel)
                if (parent / ".git").exists():
                    raise ValueError("nested git repository: " + rel)
                parents[key] = info
        target = parent / parts[-1]
        info = signature(target)
        if info is not None:
            if stat.S_ISLNK(info[0]):
                raise ValueError("symlink target: " + rel)
            if not stat.S_ISREG(info[0]) or info[5] & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
                raise ValueError("non-regular target: " + rel)
        return target, info

    def _read_snapshot_bytes(self, rel, target, expected):
        # Ancestors and the target were validated by _snapshot_target. Opening
        # with NOFOLLOW plus matching the opened file identity avoids repeating
        # safe_path's ancestor/target stat calls for every full reconciliation.
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(target, flags)
        with os.fdopen(fd, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if (not stat.S_ISREG(opened.st_mode) or opened.st_ino != expected[4]
                    or opened.st_dev != expected[6] or opened.st_size != expected[1]):
                raise ValueError("file changed while opening: " + rel)
            data = stream.read(agent.MAX_BYTES + 1)
        if len(data) > agent.MAX_BYTES:
            raise ValueError("file too large: " + rel)
        return data

    def snapshot(self, force=False, barrier=False):
        now = time.monotonic()
        hints, overflow = self.watcher.drain()
        meta = self._metadata()
        ignore = self._ignores()
        directory_sig = {str(path): signature(path) for path in self._directories}
        periodic = now - self._last_reconcile >= self.reconcile_interval
        hinted_paths = set()
        new_hint = False
        for path in hints:
            try:
                rel = Path(path).relative_to(self.root).as_posix()
                if agent.allowed(rel):
                    hinted_paths.add(rel)
                    new_hint = new_hint or rel not in self._paths
            except ValueError:
                continue
        selection = (force or barrier or self._invalidated or periodic or overflow or meta != self._meta_sig
                     or ignore != self._ignore_sig or directory_sig != self._directory_sig
                     or new_hint
                     or any(Path(path).name in (".gitignore", "exclude") for path in hints))
        if selection:
            self._validate_git_location(meta)
            if periodic or meta != self._meta_sig:
                self._refresh_exclude_config()
            self._refresh_git(metadata_changed=(force or periodic or self._git is None or meta != self._meta_sig))
        scan_ignore = self._ignores()
        scan_directories = {str(path): signature(path) for path in self._directories}
        files, excluded = {}, dict(self._listed_excluded)
        changed = False
        new_cache = {}
        parents = {}
        for rel in sorted(self._paths):
            try:
                if not agent.allowed(rel, rel in self._tracked):
                    excluded[rel] = "excluded_by_policy"
                    continue
                if any(rel == path or rel.startswith(path + "/") for path in self._submodules):
                    excluded[rel] = "submodule"
                    continue
                target, info = self._snapshot_target(rel, parents)
            except ValueError as exc:
                excluded[rel] = str(exc)
                continue
            try:
                if info is None:
                    excluded[rel] = "missing_worktree_file"
                    continue
                if info[1] > agent.MAX_BYTES:
                    excluded[rel] = "file_too_large"
                    continue
                cached = self._cache.get(rel)
                if (cached is not None and cached[0] == info and rel not in hinted_paths
                        and not (force or periodic or overflow or barrier)):
                    digest = cached[1]
                else:
                    data = self._read_snapshot_bytes(rel, target, info)
                    after = signature(target)
                    if after != info:
                        raise ValueError("file changed while scanning: " + rel)
                    digest = agent.digest(data)
                    changed = changed or cached is None or cached[1] != digest
                new_cache[rel] = (info, digest)
                files[rel] = digest
            except FileNotFoundError:
                excluded[rel] = "missing_worktree_file"
            except OSError as exc:
                raise OSError("cannot inspect/read " + rel + ": " + str(exc)) from exc
        changed = changed or set(new_cache) != set(self._cache)
        if changed and not selection:
            self._git["dirty"] = bool(agent.git(self.root, "status", "--porcelain", "-z", "--untracked-files=normal"))
        folds = {}
        for rel in files:
            folded = rel.casefold()
            if folded in folds and folds[folded] != rel:
                raise ValueError("case-insensitive path collision: " + folds[folded] + " / " + rel)
            folds[folded] = rel
        after_meta = self._metadata()
        if after_meta != meta:
            self.invalidate()
            raise ValueError("Git metadata changed while scanning")
        after_ignore = self._ignores()
        after_directories = {str(path): signature(path) for path in self._directories}
        if after_ignore != scan_ignore or after_directories != scan_directories:
            self.invalidate()
            raise ValueError("directory or ignore rules changed while scanning")
        self._cache = new_cache
        self._meta_sig = after_meta
        self._ignore_sig = after_ignore
        self._directory_sig = after_directories
        self._invalidated = False
        if force or periodic or overflow:
            self._last_reconcile = now
        result = {**self._git, "files": files,
                  "docs": {p: h for p, h in files.items() if Path(p).name in ("AGENTS.md", "CONTEXT.md")},
                  "excluded": dict(sorted(excluded.items())), "excluded_count": len(excluded)}
        result["revision"] = hashlib.sha256(json.dumps(result, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
        result["observed_at"] = time.time()
        result["watcher"] = self.watcher.mode
        return copy.deepcopy(result)

    def _guard_git(self, expected, writing=False):
        # The same stat/metadata guards as snapshot validate cached Git identity
        # and selection; a batch does not run Git once per participating file.
        self.snapshot()
        if not isinstance(expected, dict) or any(self._git[key] != expected.get(key) for key in ("head", "branch")):
            raise ValueError("git branch/HEAD changed before operation")
        if writing and (self._git["unmerged"] or self._git["git_operations"]):
            raise ValueError("git operation or unmerged index blocks writes")
        return self._metadata()

    def _scope(self, rel):
        parts = agent.path_parts(rel)
        if not agent.allowed(rel, rel in self._tracked):
            raise ValueError("excluded destination: " + rel)
        if any(rel == path or rel.startswith(path + "/") for path in self._submodules):
            raise ValueError("submodule destination: " + rel)
        parent = self.root
        # Validate existing ancestors without creating anything during preflight.
        for part in parts[:-1]:
            parent = parent / part
            info = signature(parent)
            if info is None:
                continue
            if not stat.S_ISDIR(info[0]) or parent.is_symlink() or info[5] & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
                raise ValueError("linked or invalid destination parent: " + rel)
            if (parent / ".git").exists():
                raise ValueError("nested git destination: " + rel)
        target = self.root.joinpath(*parts)
        info = signature(target)
        if info is not None and (not stat.S_ISREG(info[0]) or target.is_symlink()
                                 or info[5] & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
            raise ValueError("non-regular target: " + rel)
        if info is not None and rel not in self._paths:
            raise ValueError("destination is outside Git selection: " + rel)
        return target

    def _check_new_ignores(self, paths):
        new = [path for path in paths if path not in self._tracked]
        if not new:
            return
        proc = subprocess.run(["git", "-C", str(self.root), "check-ignore", "--stdin", "-z"],
                                            input=b"\0".join(path.encode("utf-8") for path in new) + b"\0",
                                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                            env=agent.git_environment(), timeout=30, check=False,
                                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        if proc.returncode not in (0, 1):
            raise ValueError("git check-ignore failed: " + proc.stderr.decode("utf-8", "replace"))
        if proc.stdout:
            raise ValueError("ignored destination: " + proc.stdout.split(b"\0", 1)[0].decode("utf-8", "replace"))

    def _batch_ignores(self, paths):
        result = self._ignores()
        for rel in paths:
            parent = self.root
            for part in agent.path_parts(rel)[:-1]:
                parent = parent / part
                ignore = parent / ".gitignore"
                result[str(ignore)] = signature(ignore)
        return result

    def _ignores_match(self, expected):
        return all(signature(Path(path)) == info for path, info in expected.items())

    def _current(self, rel, target):
        if signature(target) is None:
            return None, None
        data = agent.read_bytes(self.root, rel)
        return agent.digest(data), data

    def read_many(self, paths, expected_git, expected_hashes):
        self._guard_git(expected_git)
        if len(set(paths)) != len(paths) or set(paths) != set(expected_hashes):
            raise ValueError("paths and expected_hashes must match without duplicates")
        targets = {rel: self._scope(rel) for rel in paths}
        ignores = self._batch_ignores(paths)
        self._check_new_ignores(paths)
        result = {}
        for rel, target in targets.items():
            digest, data = self._current(rel, target)
            if digest != expected_hashes[rel]:
                raise ValueError("source changed: " + rel)
            result[rel] = None if data is None else {"sha256": digest, "data": base64.b64encode(data).decode("ascii")}
        # A checkout during a read cannot publish a mixed Git generation.
        meta = self._metadata()
        if meta != self._meta_sig or not self._ignores_match(ignores):
            raise ValueError("Git metadata or ignore rules changed during batch read")
        return {"files": result}

    def write_many(self, actions, expected_git, allow_delete=False):
        self._ensure_writer()
        meta = self._guard_git(expected_git, writing=True)
        paths = [item["path"] for item in actions]
        if len(set(paths)) != len(paths) or len(set(path.casefold() for path in paths)) != len(paths):
            raise ValueError("duplicate or case-colliding batch paths")
        path_set = set(path.casefold() for path in paths)
        if any("/".join(path.casefold().split("/")[:index]) in path_set
               for path in paths for index in range(1, len(path.split("/")))):
            raise ValueError("batch paths overlap as file and parent")
        folded = {path.casefold(): path for path in self._paths}
        prepared = []
        for item in actions:
            rel = item["path"]
            target = self._scope(rel)
            if rel.casefold() in folded and folded[rel.casefold()] != rel:
                raise ValueError("case-insensitive path collision: " + rel)
            data = None if item["data"] is None else base64.b64decode(item["data"], validate=True)
            if data is None and allow_delete is not True:
                raise ValueError("deletion requires allow_delete=True")
            if data is not None and len(data) > agent.MAX_BYTES:
                raise ValueError("file too large: " + rel)
            digest, _ = self._current(rel, target)
            if digest != item["expected"]:
                raise ValueError("destination changed: " + rel)
            prepared.append((rel, data, item["expected"]))
        ignores = self._batch_ignores(paths)
        self._check_new_ignores(paths)
        applied = []
        try:
            for rel, data, expected in prepared:
                if self._metadata() != meta or not self._ignores_match(ignores):
                    raise ValueError("Git metadata or ignore rules changed during batch write")
                target = self._scope(rel)
                digest, _ = self._current(rel, target)
                if digest != expected:
                    raise ValueError("destination changed during batch write: " + rel)
                if self._metadata() != meta or not self._ignores_match(ignores):
                    raise ValueError("Git metadata or ignore rules changed before applying batch action")
                if data is None:
                    if target.exists():
                        target.unlink()
                else:
                    target = agent.safe_path(self.root, rel, create_parents=True)
                    fd, temp_name = tempfile.mkstemp(prefix=".bridge-", dir=str(target.parent))
                    try:
                        with os.fdopen(fd, "wb") as stream:
                            stream.write(data)
                            stream.flush()
                            os.fsync(stream.fileno())
                        if target.exists():
                            os.chmod(temp_name, stat.S_IMODE(target.stat().st_mode))
                        # Verify CAS again after writing/fsyncing the temporary
                        # file, immediately before replacing the destination.
                        current, _ = self._current(rel, self._scope(rel))
                        if current != expected or self._metadata() != meta or not self._ignores_match(ignores):
                            raise ValueError("destination or Git changed before replacement: " + rel)
                        os.replace(temp_name, target)
                    finally:
                        if os.path.exists(temp_name):
                            os.unlink(temp_name)
                applied.append(rel)
                if Path(rel).name == ".gitignore":
                    # The batch may legitimately synchronize its ignore file.
                    # Accept our own update, then validate remaining untracked
                    # destinations against the resulting selection policy.
                    ignores[str(self.root.joinpath(*agent.path_parts(rel)))] = signature(target)
                    remaining = [item[0] for item in prepared if item[0] not in applied]
                    self._check_new_ignores(remaining)
        except Exception as exc:
            raise RuntimeError("batch write failed; applied=" + json.dumps(applied) + ": " + str(exc)) from exc
        finally:
            self.invalidate()
        return {"applied": applied}

    def dispatch(self, request):
        if request.get("root") != self._root_value:
            raise ValueError("peer root is fixed")
        op = request["op"]
        if op in ("snapshot", "inventory"):
            return self.snapshot(force=request.get("force", False), barrier=request.get("barrier", False))
        if op == "claim_controller":
            return self.claim_controller(request["instance_id"])
        if op == "read_many":
            return self.read_many(request["paths"], request["expected_git"], request["expected_hashes"])
        if op == "write_many":
            return self.write_many(request["actions"], request["expected_git"], request.get("allow_delete", False))
        if op.startswith("git_"):
            from . import git_ops
            if op not in ("git_status", "git_is_descendant", "git_tag_check", "git_bundle_create"):
                self._ensure_writer()
            try:
                return git_ops.dispatch(self.root, op, request)
            finally:
                self.invalidate()
        raise ValueError("unknown operation: " + op)


def serve(root, reconcile_interval=30):
    runtime = None
    try:
        for line in sys.stdin.buffer:
            request_id = None
            try:
                request = json.loads(line)
                request_id = request["id"]
                if runtime is None:
                    runtime = Runtime(root, reconcile_interval=reconcile_interval)
                response = {"id": request_id, "ok": True, "result": runtime.dispatch(request)}
            except Exception as exc:
                response = {"id": request_id, "ok": False, "error": str(exc), "error_type": type(exc).__name__}
            sys.stdout.write(json.dumps(response, ensure_ascii=True) + "\n")
            sys.stdout.flush()
    finally:
        if runtime is not None:
            runtime.close()
