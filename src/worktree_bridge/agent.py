"""One-shot endpoint operations. This file is also run through SSH stdin/stdout."""

import base64
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile

MAX_BYTES = 1024 * 1024
SKIP_DIRS = {".git", ".ssh", ".aws", ".azure", ".gnupg", ".venv", "venv", "node_modules", "checkpoints", "logs", "wandb", "runs", "outputs", "__pycache__"}
DATA_DIRS = {"data", "datasets"}
SOURCE_SUFFIXES = {".py", ".pyi", ".sh", ".ps1", ".js", ".ts", ".c", ".cc", ".cpp", ".h", ".hpp", ".cu", ".cuh"}
SKIP_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".ckpt", ".pt", ".pth", ".safetensors", ".onnx", ".h5", ".hdf5", ".npy", ".npz", ".parquet", ".sqlite", ".db", ".log", ".zip", ".tar", ".gz", ".7z", ".mp4", ".mov"}
SECRET_NAMES = {"id_rsa", "id_ed25519", "id_dsa", "id_ecdsa", "authorized_keys", "credentials", "credentials.json", "secrets.json"}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def git(root, *args):
    env = dict(os.environ)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    proc = subprocess.run(["git", "-C", str(root), *args], stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, timeout=30, check=False, env=env)
    if proc.returncode:
        raise ValueError("git command failed: " + proc.stderr.decode("utf-8", "replace").strip())
    return proc.stdout


def ignored(root, rel):
    env = dict(os.environ)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    proc = subprocess.run(["git", "-C", str(root), "check-ignore", "-q", "--", rel],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
                          check=False, env=env)
    if proc.returncode not in (0, 1):
        raise ValueError("git check-ignore failed: " + proc.stderr.decode("utf-8", "replace").strip())
    return proc.returncode == 0


def selected(root, rel):
    listed = git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard",
                 "--", ":(literal)" + rel)
    return rel.encode("utf-8") in listed.split(b"\x00")


def tracked(root, rel):
    listed = git(root, "ls-files", "-z", "--cached", "--", ":(literal)" + rel)
    return rel.encode("utf-8") in listed.split(b"\x00")


def check_target(root, rel):
    if not allowed(rel):
        raise ValueError("excluded destination: " + rel)
    if ignored(root, rel):
        raise ValueError("ignored destination: " + rel)
    parent = root
    parts = path_parts(rel)
    for part in parts[:-1]:
        parent = parent / part
        if parent.exists() or parent.is_symlink():
            if not parent.is_dir() or parent.is_symlink():
                raise ValueError("linked or invalid destination parent: " + rel)
            attrs = getattr(parent.lstat(), "st_file_attributes", 0)
            if attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
                raise ValueError("reparse destination parent: " + rel)
            if (parent / ".git").exists():
                raise ValueError("nested git destination: " + rel)
    target = root.joinpath(*parts)
    if target.exists() or target.is_symlink():
        raise ValueError("unselected destination already exists: " + rel)


def root_path(value):
    root = Path(value)
    if not root.is_absolute() or not root.is_dir() or root.is_symlink():
        raise ValueError("root must be an existing absolute directory without a link")
    attrs = getattr(root.lstat(), "st_file_attributes", 0)
    if attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
        raise ValueError("root is a reparse point")
    return root


def path_parts(rel):
    if not isinstance(rel, str) or not rel or "\\" in rel or "\x00" in rel:
        raise ValueError("unsafe relative path")
    parts = rel.split("/")
    reserved = {"con", "prn", "aux", "nul"} | {"com" + str(i) for i in range(1, 10)} | {"lpt" + str(i) for i in range(1, 10)}
    if any(part in ("", ".", "..") or part.lower() == ".git" or ":" in part
           or part.endswith((".", " ")) or part.split(".", 1)[0].lower() in reserved
           or any(ord(c) < 32 for c in part) for part in parts):
        raise ValueError("unsafe relative path")
    return parts


def safe_path(root, rel, create_parents=False):
    parts = path_parts(rel)
    parent = root
    for part in parts[:-1]:
        parent = parent / part
        if create_parents and not parent.exists() and not parent.is_symlink():
            parent.mkdir()
        if not parent.is_dir() or parent.is_symlink():
            raise ValueError("linked or invalid parent: " + rel)
        attrs = getattr(parent.lstat(), "st_file_attributes", 0)
        if attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
            raise ValueError("reparse parent: " + rel)
        if (parent / ".git").exists():
            raise ValueError("nested git repository: " + rel)
    target = parent / parts[-1]
    if target.is_symlink():
        raise ValueError("symlink target: " + rel)
    if target.exists():
        mode = target.lstat().st_mode
        attrs = getattr(target.lstat(), "st_file_attributes", 0)
        if not stat.S_ISREG(mode) or attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
            raise ValueError("non-regular target: " + rel)
    return target


def allowed(rel, is_tracked=False):
    parts = path_parts(rel)
    low = [p.lower() for p in parts]
    if any(p in SKIP_DIRS for p in low[:-1]):
        return False
    name = low[-1]
    if any(p in DATA_DIRS for p in low[:-1]) and not (is_tracked and Path(name).suffix in SOURCE_SUFFIXES):
        return False
    if (name.startswith(".env") or name in SECRET_NAMES
            or name.startswith(("id_rsa", "id_ed25519", "secret", "password", "api_key", "private_key"))
            or name.endswith("_private")
            or Path(name).suffix in SKIP_SUFFIXES):
        return False
    return True


def read_bytes(root, rel):
    target = safe_path(root, rel)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(target, flags)
    with os.fdopen(fd, "rb") as f:
        data = f.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("file too large: " + rel)
    return data


def inventory(root):
    top = git(root, "rev-parse", "--show-toplevel").decode("utf-8", "surrogateescape").strip()
    if Path(top).resolve() != root.resolve():
        raise ValueError("root must be the git worktree top level")
    head = git(root, "rev-parse", "HEAD").decode("ascii").strip()
    branch = git(root, "branch", "--show-current").decode("utf-8", "replace").strip()
    dirty = bool(git(root, "status", "--porcelain", "-z", "--untracked-files=normal"))
    git_dir = Path(git(root, "rev-parse", "--absolute-git-dir").decode("utf-8", "replace").strip())
    operations = []
    for marker in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "REBASE_HEAD",
                   "rebase-merge", "rebase-apply", "sequencer", "BISECT_LOG"):
        if (git_dir / marker).exists():
            operations.append(marker)
    staged = git(root, "ls-files", "--stage", "-z")
    submodules = set()
    tracked_paths = set()
    for entry in staged.split(b"\x00"):
        if not entry:
            continue
        try:
            staged_path = entry.split(b"\t", 1)[1].decode("utf-8")
        except UnicodeError:
            continue
        tracked_paths.add(staged_path)
        if entry.startswith(b"160000 "):
            submodules.add(staged_path)
    listed = git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    files = {}
    excluded = {}
    for raw in set(listed.split(b"\x00")) - {b""}:
        try:
            rel = raw.decode("utf-8")
        except UnicodeError:
            excluded["<bytes:" + raw.hex() + ">"] = "invalid_utf8_path"
            continue
        try:
            if not allowed(rel, rel in tracked_paths):
                excluded[rel] = "excluded_by_policy"
                continue
            if any(rel == s or rel.startswith(s + "/") for s in submodules):
                excluded[rel] = "submodule"
                continue
            target = safe_path(root, rel)
        except ValueError as exc:
            excluded[rel] = str(exc)
            continue
        try:
            info = target.lstat()
        except FileNotFoundError:
            excluded[rel] = "missing_worktree_file"
            continue
        except OSError as exc:
            raise OSError("cannot inspect " + rel + ": " + str(exc)) from exc
        if not stat.S_ISREG(info.st_mode):
            excluded[rel] = "non_regular_file"
            continue
        if info.st_size > MAX_BYTES:
            excluded[rel] = "file_too_large"
            continue
        try:
            files[rel] = digest(read_bytes(root, rel))
        except FileNotFoundError:
            excluded[rel] = "missing_worktree_file"
            continue
        except OSError as exc:
            raise OSError("cannot read " + rel + ": " + str(exc)) from exc
    folds = {}
    for rel in files:
        folded = rel.casefold()
        if folded in folds and folds[folded] != rel:
            raise ValueError("case-insensitive path collision: " + folds[folded] + " / " + rel)
        folds[folded] = rel
    docs = {p: h for p, h in sorted(files.items()) if Path(p).name in ("AGENTS.md", "CONTEXT.md")}
    return {"head": head, "branch": branch, "dirty": dirty,
            "files": dict(sorted(files.items())), "docs": docs,
            "excluded": dict(sorted(excluded.items())), "excluded_count": len(excluded),
            "git_operations": operations,
            "unmerged": sorted({entry.split(b"\t", 1)[1].decode("utf-8", "replace")
                                for entry in git(root, "ls-files", "--unmerged", "-z").split(b"\x00") if entry})}


def write_bytes(root, rel, data, expected):
    if not allowed(rel, tracked(root, rel)) or len(data) > MAX_BYTES:
        raise ValueError("excluded or oversized path: " + rel)
    if expected is None:
        check_target(root, rel)
    target = safe_path(root, rel, create_parents=True)
    old = read_bytes(root, rel) if target.exists() else None
    if (digest(old) if old is not None else None) != expected:
        raise ValueError("destination changed: " + rel)
    fd, temp_name = tempfile.mkstemp(prefix=".bridge-", dir=str(target.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if target.exists():
            os.chmod(temp_name, stat.S_IMODE(target.stat().st_mode))
        os.replace(temp_name, target)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    return digest(data)


def dispatch(req):
    root = root_path(req["root"])
    op = req["op"]
    if op == "inventory":
        return inventory(root)
    if op in ("read", "write", "check_target"):
        if (git(root, "rev-parse", "HEAD").decode("ascii").strip() != req["expect_head"]
                or git(root, "branch", "--show-current").decode("utf-8").strip() != req["expect_branch"]):
            raise ValueError("git branch/HEAD changed before " + op)
    if op == "read":
        if not selected(root, req["path"]) or not allowed(req["path"], tracked(root, req["path"])):
            raise ValueError("path is outside Git selection: " + req["path"])
        data = read_bytes(root, req["path"])
        return {"data": base64.b64encode(data).decode("ascii"), "sha256": digest(data)}
    if op == "check_target":
        check_target(root, req["path"])
        return {"writable": True}
    if op == "write":
        if req["expected"] is not None and not selected(root, req["path"]):
            raise ValueError("destination is outside Git selection: " + req["path"])
        data = base64.b64decode(req["data"], validate=True)
        return {"sha256": write_bytes(root, req["path"], data, req["expected"])}
    raise ValueError("unknown operation")


def serve():
    try:
        req = json.loads(sys.stdin.buffer.read())
        result = {"ok": True, "result": dispatch(req)}
    except Exception as exc:
        result = {"ok": False, "error": str(exc)}
    sys.stdout.write(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    serve()
