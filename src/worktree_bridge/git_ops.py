"""Bounded native Git operations, also bundled into the Python 3.9 peer."""

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import uuid

from . import agent

MAX_BUNDLE_BYTES = 8 * 1024 * 1024
MAX_HISTORY_COMMITS = 128
MAX_HISTORY_PATHS = 4096
_OID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_MARKERS = ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "REBASE_HEAD",
            "rebase-merge", "rebase-apply", "sequencer", "BISECT_LOG")


def _git(root, *args, **kwargs):
    env = agent.git_environment()
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    if kwargs.get("index") is not None:
        env["GIT_INDEX_FILE"] = str(kwargs["index"])
    proc = subprocess.run(["git", "-C", str(root), *args],
                          input=kwargs.get("input"), stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, env=env, timeout=30,
                          creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    if proc.returncode:
        raise ValueError("git command failed: " + proc.stderr.decode("utf-8", "replace").strip())
    return proc.stdout


def _oid(value):
    if not isinstance(value, str) or not _OID.fullmatch(value):
        raise ValueError("invalid Git object id")
    return value


def _paths(data):
    return [p.decode("utf-8") for p in data.split(b"\0") if p]


def _index_path(root):
    value = _git(root, "rev-parse", "--git-path", "index").decode("utf-8").strip()
    path = Path(value)
    return path if path.is_absolute() else root / path


def _digest_path(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def _state(root):
    records = _git(root, "status", "--porcelain=v2", "--branch", "-z", "--untracked-files=normal").split(b"\0")
    head = branch = None
    staged = False
    unmerged = []
    dirty_paths = []
    skip_original = False
    for entry in records:
        if skip_original:
            skip_original = False
            continue
        if entry.startswith(b"# branch.oid "):
            head = entry[13:].decode("ascii")
        elif entry.startswith(b"# branch.head "):
            branch = entry[14:].decode("utf-8")
        elif entry.startswith((b"1 ", b"2 ")):
            skip_original = entry.startswith(b"2 ")
            fields = entry.split(b" ", 9 if skip_original else 8)
            # Intent-to-add is invisible in the usual cached diff but belongs
            # to the user's index and must never be discarded by rebuilding it.
            staged = staged or fields[1][:1] != b"." or fields[4] == b"000000"
            dirty_paths.append(fields[-1].decode("utf-8"))
        elif entry.startswith(b"u "):
            unmerged.append(entry.split(b" ", 10)[10].decode("utf-8"))
        elif entry.startswith(b"? "):
            dirty_paths.append(entry[2:].decode("utf-8"))
    if not head or not _OID.fullmatch(head) or not branch or branch == "(detached)":
        raise ValueError("Git coordination requires an existing commit and named branch")
    directory = Path(_git(root, "rev-parse", "--absolute-git-dir").decode("utf-8").strip())
    index = directory / "index"
    operations = [name for name in _MARKERS if (directory / name).exists()]
    if Path(str(index) + ".lock").exists():
        operations.append("index.lock")
    flagged = []
    for entry in _git(root, "ls-files", "-v", "-z").split(b"\0"):
        if entry and (entry[:1].islower() or entry[:1] == b"S"):
            flagged.append(entry[2:].decode("utf-8"))
    return {"head": head, "branch": branch, "staged": staged, "unmerged": unmerged, "dirty_paths": dirty_paths,
            "git_operations": operations, "index_flags": flagged,
            "index_hash": _digest_path(index)}


def _expect(root, expected, allow_lock=False):
    state = _state(root)
    if state["head"] != expected["head"] or state["branch"] != expected["branch"]:
        raise ValueError("Git HEAD/branch changed")
    operations = [p for p in state["git_operations"] if not (allow_lock and p == "index.lock")]
    if state["staged"] or state["unmerged"] or operations or state["index_flags"]:
        raise ValueError("staged work, index flags or Git operation blocks automatic Git changes")
    return state


def _ancestor(root, older, newer):
    env = agent.git_environment()
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    proc = subprocess.run(["git", "-C", str(root), "merge-base", "--is-ancestor", _oid(older), _oid(newer)],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, env=env,
                          creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    if proc.returncode not in (0, 1):
        raise ValueError("cannot establish Git ancestry")
    return proc.returncode == 0


def _tag_ref(root, tag):
    if not isinstance(tag, str) or not tag or len(tag) > 200:
        raise ValueError("invalid tag")
    ref = "refs/tags/" + tag
    _git(root, "check-ref-format", ref)
    return ref


def _tag_check(root, tag):
    ref = _tag_ref(root, tag)
    proc = agent.git_process(root, "show-ref", "--verify", "--quiet", ref)
    if proc.returncode not in (0, 1):
        raise ValueError("cannot inspect tag")
    return {"exists": proc.returncode == 0,
            "head": _git(root, "show-ref", "--verify", "--hash", ref).decode("ascii").strip() if proc.returncode == 0 else None}


class _Journal:
    def __init__(self, root, operation, details):
        directory = Path(_git(root, "rev-parse", "--absolute-git-dir").decode("utf-8").strip())
        directory = directory / "worktree-bridge-journal"
        directory.mkdir(exist_ok=True)
        self.path = directory / (uuid.uuid4().hex + ".json")
        self.data = {"schema": 1, "operation": operation, "status": "running",
                     "phase": "preflight", "created_at": time.time(), **details}
        self.save()

    def save(self, **updates):
        self.data.update(updates)
        self.data["updated_at"] = time.time()
        fd, temp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".journal-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(self.data, stream, ensure_ascii=False, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, str(self.path))
        finally:
            if os.path.exists(temp):
                os.unlink(temp)


def _selected(root, expected):
    current = agent.inventory(root)["files"]
    if current != expected:
        raise ValueError("working file hashes changed")
    return current


def _tree(root, head):
    result = {}
    for raw in _git(root, "ls-tree", "-r", "-z", "--full-tree", _oid(head)).split(b"\0"):
        if raw:
            metadata, path = raw.split(b"\t", 1)
            mode, kind, oid = metadata.decode("ascii").split(" ")
            result[path.decode("utf-8")] = {"mode": mode, "kind": kind, "oid": oid}
    return result


def _blob_hash(root, entry, path):
    if entry is None:
        return None
    if entry["mode"] not in ("100644", "100755") or entry["kind"] != "blob":
        raise ValueError("unsupported incoming Git path: " + path)
    if not agent.allowed(path, True):
        raise ValueError("excluded incoming Git path: " + path)
    size = int(_git(root, "cat-file", "-s", entry["oid"]))
    if size > agent.MAX_BYTES:
        raise ValueError("oversized incoming Git path: " + path)
    return agent.digest(_git(root, "cat-file", "blob", entry["oid"]))


def _work_hash(root, path, unbounded=False):
    parts = agent.path_parts(path)
    current = root
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("linked Git path: " + path)
        if not current.exists():
            return None
    if not unbounded:
        return agent.digest(agent.read_bytes(root, path))
    target = agent.safe_path(root, path)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(str(target), flags)
    digest = hashlib.sha256()
    with os.fdopen(descriptor, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _preserved(root, expected):
    for path, value in expected.items():
        if _work_hash(root, path, unbounded=not agent.allowed(path, True)) != value:
            raise ValueError("working file changed during Git transaction: " + path)


def _transaction(root, old, new, expected_files, journal, prepare, tag=None):
    """Hold Git's index lock from preparation through CAS and index publication.

    Git produces the temporary index; no working file or index is reconstructed
    by this module. A failure after the ref transaction leaves index.lock and
    the durable phase record for inspection rather than releasing a stale index.
    """
    original = _expect(root, old)
    index = _index_path(root)
    lock = Path(str(index) + ".lock")
    fd = os.open(str(lock), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    committed = False
    published = False
    ref_attempted = False
    fd, temp_name = tempfile.mkstemp(dir=str(index.parent), prefix=".wtb-index-")
    os.close(fd)
    os.unlink(temp_name)
    temporary = Path(temp_name)
    try:
        journal.save(phase="index_locked", index_lock=str(lock), before_index=original["index_hash"])
        prepare(temporary)
        _preserved(root, expected_files)
        now = _expect(root, old, allow_lock=True)
        if now["index_hash"] != original["index_hash"]:
            raise ValueError("index changed while preparing Git transaction")
        with temporary.open("r+b") as stream:
            os.fsync(stream.fileno())
        os.replace(str(temporary), str(lock))
        journal.save(phase="index_prepared", prepared_index=_digest_path(lock))
        ref = "refs/heads/" + old["branch"]
        commands = "start\nupdate " + ref + " " + new + " " + old["head"] + "\n"
        if tag is not None:
            commands += "create " + _tag_ref(root, tag) + " " + new + "\n"
        commands += "prepare\ncommit\n"
        ref_attempted = True
        _git(root, "update-ref", "-m", "worktree-bridge " + journal.data["operation"],
             "--stdin", input=commands.encode("utf-8"))
        committed = True
        journal.save(phase="ref_updated", after_head=new)
        os.replace(str(lock), str(index))
        published = True
        journal.save(phase="index_published", after_index=_digest_path(index))
        _preserved(root, expected_files)
        state = _expect(root, {"head": new, "branch": old["branch"]})
        journal.save(status="complete", phase="verified", after_files=expected_files,
                     after_index=state["index_hash"])
    finally:
        if ref_attempted and not committed:
            # A command timeout/error can arrive after Git committed the ref.
            # Inspect rather than releasing the lock on an unknown outcome.
            try:
                observed = _git(root, "rev-parse", "--verify", "refs/heads/" + old["branch"]).decode("ascii").strip()
                committed = observed != old["head"]
            except Exception:
                committed = True
        if temporary.exists():
            temporary.unlink()
        alt_lock = Path(str(temporary) + ".lock")
        if alt_lock.exists():
            alt_lock.unlink()
        if not committed and lock.exists():
            lock.unlink()
        if committed and not published:
            journal.save(recovery="ref advanced; original index preserved, prepared index.lock retained; inspect before resuming")


def _bundle_create(root, request):
    expected = request["expected_git"]
    _expect(root, expected)
    base = _oid(request["base_head"])
    if not _ancestor(root, base, expected["head"]) or base == expected["head"]:
        raise ValueError("incoming commit must descend from the common baseline")
    # A bundle contains the whole new reachable history, including files added
    # and removed before its final tree. Audit before any bytes leave this peer.
    commits = _git(root, "rev-list", "--max-count=" + str(MAX_HISTORY_COMMITS + 1),
                   base + ".." + expected["head"]).decode("ascii").splitlines()
    if len(commits) > MAX_HISTORY_COMMITS:
        raise ValueError("incoming Git history exceeds automatic-review commit limit")
    checked_blobs = set()
    path_count = 0
    for commit in commits:
        raw = _git(root, "diff-tree", "--root", "-m", "-r", "--no-commit-id",
                   "--no-renames", "--raw", "-z", commit).split(b"\0")
        paths = set()
        for offset in range(0, len(raw) - 1, 2):
            modes = raw[offset].split(b" ", 2)[:2]
            old_mode, new_mode = modes[0][1:], modes[1]
            path = raw[offset + 1].decode("utf-8")
            if old_mode not in (b"000000", b"100644", b"100755") or new_mode not in (b"000000", b"100644", b"100755"):
                raise ValueError("unsupported path in incoming Git history: " + path)
            if old_mode != b"000000" and new_mode != b"000000" and old_mode != new_mode:
                raise ValueError("mode change in incoming Git history is outside byte synchronization: " + path)
            paths.add(path)
        path_count += len(paths)
        if path_count > MAX_HISTORY_PATHS:
            raise ValueError("incoming Git history exceeds automatic-review path limit")
        tree = _tree(root, commit)
        for path in paths:
            agent.path_parts(path)
            if not agent.allowed(path, True):
                raise ValueError("excluded path in incoming Git history: " + path)
            entry = tree.get(path)
            blob_key = (entry["mode"], entry["kind"], entry["oid"]) if entry else None
            if entry is not None and blob_key not in checked_blobs:
                _blob_hash(root, entry, path)
                checked_blobs.add(blob_key)
    with tempfile.TemporaryDirectory(prefix="wtb-bundle-") as directory:
        path = Path(directory) / "objects.bundle"
        _git(root, "bundle", "create", str(path), "refs/heads/" + expected["branch"], "^" + base)
        if path.stat().st_size > MAX_BUNDLE_BYTES:
            raise ValueError("Git bundle exceeds transfer limit")
        data = path.read_bytes()
    _expect(root, expected)
    return {"data": base64.b64encode(data).decode("ascii"), "size": len(data), **expected}


def _bundle_import(root, request):
    _expect(root, request["expected_git"])
    incoming = _oid(request["incoming_head"])
    base = _oid(request["base_head"])
    encoded = request["data"]
    if not isinstance(encoded, str) or len(encoded) > ((MAX_BUNDLE_BYTES + 2) // 3) * 4:
        raise ValueError("Git bundle exceeds transfer limit")
    data = base64.b64decode(encoded, validate=True)
    if len(data) > MAX_BUNDLE_BYTES:
        raise ValueError("Git bundle exceeds transfer limit")
    with tempfile.TemporaryDirectory(prefix="wtb-bundle-") as directory:
        path = Path(directory) / "objects.bundle"
        path.write_bytes(data)
        heads = _git(root, "bundle", "list-heads", str(path)).decode("utf-8").splitlines()
        expected_ref = incoming + " refs/heads/" + request["expected_git"]["branch"]
        if heads != [expected_ref]:
            raise ValueError("bundle advertised branch/HEAD does not match")
        _git(root, "bundle", "verify", str(path))
        _git(root, "bundle", "unbundle", str(path))
    if not _ancestor(root, base, incoming):
        raise ValueError("imported commit is not a baseline descendant")
    _expect(root, request["expected_git"])
    return {"head": incoming}


def _follow(root, request):
    old = request["expected_git"]
    state = _expect(root, old)
    incoming = _oid(request["incoming_head"])
    if old["head"] != request["base_head"] or incoming == old["head"] or not _ancestor(root, old["head"], incoming):
        raise ValueError("only one-sided descendant fast-forward is permitted")
    before = _selected(root, request["expected_files"])
    journal = _Journal(root, "follow", {"before_head": old["head"], "incoming_head": incoming,
                       "branch": old["branch"], "before_files": before, "before_index": state["index_hash"]})
    try:
        old_tree, new_tree = _tree(root, old["head"]), _tree(root, incoming)
        folded = {}
        for path in new_tree:
            if path.casefold() in folded and folded[path.casefold()] != path:
                raise ValueError("incoming tree has a case-insensitive path collision")
            folded[path.casefold()] = path
        changed = sorted(p for p in set(old_tree) | set(new_tree) if old_tree.get(p) != new_tree.get(p))
        incoming_hashes = {}
        for path in changed:
            agent.path_parts(path)
            if not agent.allowed(path, True):
                raise ValueError("excluded incoming Git path: " + path)
            if old_tree.get(path) and new_tree.get(path) and old_tree[path]["mode"] != new_tree[path]["mode"]:
                raise ValueError("incoming mode change is outside byte synchronization: " + path)
            incoming_hashes[path] = _blob_hash(root, new_tree.get(path), path)
            _blob_hash(root, old_tree.get(path), path)
        dirty = set(_paths(_git(root, "diff", "--name-only", "-z", "HEAD", "--")))
        dirty.update(_paths(_git(root, "ls-files", "--others", "--exclude-standard", "-z")))
        baseline = request["baseline_files"]
        protected = {}
        for path in dirty:
            # Source history was audited before transfer. Untouched private
            # paths retain both their old index entry and their working bytes.
            if path not in incoming_hashes and not agent.allowed(path, path in old_tree):
                protected[path] = _work_hash(root, path, unbounded=True)
                continue
            value = _work_hash(root, path)
            if not (path in baseline and baseline[path] == value) and not (path in incoming_hashes and incoming_hashes[path] == value):
                raise ValueError("independent dirty work blocks Git following: " + path)
        actual = {path: _work_hash(root, path) for path in changed}
        for path, value in actual.items():
            if value != baseline.get(path) and value != incoming_hashes[path]:
                raise ValueError("target differs from common baseline and incoming tree: " + path)
        if request.get("tag") is not None and _tag_check(root, request["tag"])["exists"]:
            raise ValueError("tag already exists")
        preserved = dict(before)
        preserved.update(actual)
        preserved.update(protected)
        journal.save(protected_files=protected)
        def prepare(index):
            _git(root, "read-tree", old["head"], index=index)
            _git(root, "read-tree", "-i", "-m", old["head"], incoming, index=index)
        _transaction(root, old, incoming, preserved, journal, prepare, request.get("tag"))
        method = "preserved-worktree-index"
        after = agent.inventory(root)["files"]
        return {"head": incoming, "branch": old["branch"], "method": method,
                "journal": str(journal.path), "before_files": before, "after_files": after}
    except Exception as exc:
        journal.save(status="failed", error=str(exc), observed_head=_state(root)["head"],
                     observed_index=_digest_path(_index_path(root)))
        raise ValueError(str(exc) + "; journal: " + str(journal.path)) from exc


def _checkpoint(root, request):
    old = request["expected_git"]
    state = _expect(root, old)
    expected = _selected(root, request["expected_files"])
    message = request["message"]
    if not isinstance(message, str) or not message.strip() or len(message.encode("utf-8")) > 16 * 1024 or "\0" in message:
        raise ValueError("checkpoint needs a nonempty bounded commit message")
    # Require an explicitly configured Git identity, never synthesize one.
    for setting in ("user.name", "user.email"):
        identity = agent.git_process(root, "config", "--get", setting)
        if identity.returncode or not identity.stdout.decode("utf-8").strip():
            raise ValueError("Git identity is missing: " + setting)
    tag = request.get("tag")
    if tag is not None and _tag_check(root, tag)["exists"]:
        raise ValueError("tag already exists")
    tracked = set(_paths(_git(root, "ls-files", "--cached", "-z")))
    candidates = set(_paths(_git(root, "diff", "--name-only", "-z", "HEAD", "--")))
    candidates.update(_paths(_git(root, "ls-files", "--others", "--exclude-standard", "-z")))
    paths = sorted(p for p in candidates if agent.allowed(p, p in tracked)
                   and (p in expected or (p in tracked and _work_hash(root, p) is None)))
    if not paths:
        raise ValueError("no eligible changes to checkpoint")
    protected = {p: _work_hash(root, p, unbounded=True) for p in candidates if not agent.allowed(p, p in tracked)}
    preserved = dict(expected)
    preserved.update(protected)
    journal = _Journal(root, "checkpoint", {"before_head": old["head"], "branch": old["branch"],
                       "before_files": expected, "before_index": state["index_hash"],
                       "paths": paths, "tag": tag, "message": message, "protected_files": protected})
    try:
        with tempfile.TemporaryDirectory(prefix="wtb-checkpoint-") as directory:
            index = Path(directory) / "index"
            _git(root, "read-tree", old["head"], index=index)
            _git(root, "add", "--", *[":(literal)" + p for p in paths], index=index)
            tree = _git(root, "write-tree", index=index).decode("ascii").strip()
            _selected(root, expected)
            _expect(root, old)
            new = _git(root, "commit-tree", tree, "-p", old["head"],
                       input=(message.rstrip() + "\n").encode("utf-8")).decode("ascii").strip()
        journal.save(phase="commit_created", incoming_head=new)
        def prepare(index):
            _git(root, "read-tree", old["head"], index=index)
            _git(root, "read-tree", "-i", "-m", old["head"], new, index=index)
        _transaction(root, old, new, preserved, journal, prepare, tag)
        return {"head": new, "branch": old["branch"], "tag": tag, "journal": str(journal.path)}
    except Exception as exc:
        journal.save(status="failed", error=str(exc), observed_head=_state(root)["head"],
                     observed_index=_digest_path(_index_path(root)))
        raise ValueError(str(exc) + "; journal: " + str(journal.path)) from exc


def dispatch(root, op, request):
    root = agent.root_path(str(root))
    top = Path(_git(root, "rev-parse", "--show-toplevel").decode("utf-8").strip())
    if top.resolve() != root.resolve():
        raise ValueError("root must be the Git worktree top level")
    operations = {"git_status": lambda: _state(root),
                  "git_is_descendant": lambda: {"descendant": _ancestor(root, request["base_head"], request["incoming_head"])},
                  "git_bundle_create": lambda: _bundle_create(root, request),
                  "git_bundle_import": lambda: _bundle_import(root, request),
                  "git_follow": lambda: _follow(root, request),
                  "git_checkpoint": lambda: _checkpoint(root, request),
                  "git_tag_check": lambda: _tag_check(root, request["tag"])}
    if op not in operations:
        raise ValueError("unknown Git operation")
    return operations[op]()
