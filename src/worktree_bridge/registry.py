"""Register an existing Git worktree without changing its tracked files."""

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import tempfile
import time

from .agent import git_environment


def _absolute(path):
    return Path(os.path.abspath(os.fspath(path)))


def _reject_links(path):
    """Check before resolving: resolve() alone would hide metadata links."""
    path = _absolute(path)
    for item in reversed((path, *path.parents)):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise RuntimeError("Git metadata or registration path is a symlink/reparse point: " + str(item))


def _metadata(cwd):
    current = _absolute(cwd)
    _reject_links(current)
    # Expand Windows 8.3 aliases only after checking the original path for links.
    current = current.resolve()
    if not current.is_dir():
        raise RuntimeError("Project directory does not exist: " + str(current))
    for root in (current, *current.parents):
        marker = root / ".git"
        try:
            info = marker.lstat()
        except FileNotFoundError:
            continue
        _reject_links(marker)
        if stat.S_ISDIR(info.st_mode):
            return root, marker.resolve()
        if not stat.S_ISREG(info.st_mode):
            raise RuntimeError("Invalid Git metadata marker: " + str(marker))
        try:
            text = marker.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError) as exc:
            raise RuntimeError("Cannot read Git metadata: " + str(marker)) from exc
        if not text.startswith("gitdir: ") or "\n" in text or "\r" in text or "\x00" in text:
            raise RuntimeError("Invalid Git gitdir file: " + str(marker))
        target = Path(text[len("gitdir: "):])
        gitdir = _absolute(target if target.is_absolute() else root / target)
        _reject_links(gitdir)
        gitdir = gitdir.resolve()
        if not gitdir.is_dir():
            raise RuntimeError("Git gitdir does not exist: " + str(gitdir))
        return root, gitdir
    raise RuntimeError("This directory is not inside an existing Git worktree; run git init first")


def _config_path(gitdir):
    path = gitdir / "worktree-bridge" / "config.json"
    _reject_links(path)
    return path


def _read_json(path, label):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise RuntimeError("Cannot read " + label + ": " + str(path) + ": " + str(exc)) from exc
    if not isinstance(value, dict):
        raise RuntimeError(label + " must contain a JSON object: " + str(path))
    return value


def _remote(remote, local_root):
    if not isinstance(remote, dict) or remote.get("kind") not in {"local", "ssh"}:
        raise RuntimeError("remote must specify kind=local or kind=ssh")
    value = dict(remote)
    root = value.get("root")
    if not isinstance(root, str) or not root or any(char in root for char in "\x00\n\r"):
        raise RuntimeError("remote.root must be an absolute path")
    if value["kind"] == "local":
        remote_root = Path(root)
        if not remote_root.is_absolute():
            raise RuntimeError("local remote.root must be absolute")
        remote_root = remote_root.resolve()
        if remote_root == local_root or local_root in remote_root.parents or remote_root in local_root.parents:
            raise RuntimeError("local and remote roots must be separate worktrees")
        value["root"] = str(remote_root)
    else:
        if not PurePosixPath(root).is_absolute():
            raise RuntimeError("SSH remote.root must be an absolute POSIX path")
        host = value.get("host")
        if not isinstance(host, str) or not host or host.startswith("-") or any(char.isspace() or ord(char) < 32 for char in host):
            raise RuntimeError("remote.host must be a host or SSH alias, without whitespace or options")
        port = value.get("port")
        if port is not None and (type(port) is not int or not 1 <= port <= 65535):
            raise RuntimeError("remote.port must be an integer between 1 and 65535")
    return value


def _outside_worktrees(state, local_root, remote):
    if not state.is_absolute():
        raise RuntimeError("state_dir must be absolute")
    resolved = state.resolve()
    roots = [local_root]
    if remote["kind"] == "local":
        roots.append(Path(remote["root"]).resolve())
    if any(resolved == root or root in resolved.parents for root in roots):
        raise RuntimeError("state_dir and registry must be outside both worktrees")


def _validate_config(config, root):
    local = config.get("local_root")
    if not isinstance(local, str) or not Path(local).is_absolute() or Path(local).resolve() != root:
        raise RuntimeError("Configuration local_root must equal the current Git worktree")
    remote = _remote(config.get("remote"), root)
    state = config.get("state_dir")
    if not isinstance(state, str):
        raise RuntimeError("Configuration state_dir must be an absolute path")
    _outside_worktrees(Path(state), root, remote)
    _reject_links(Path(state))


def _registry_root():
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return _absolute(base / "WorktreeBridge")
    base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return _absolute(base / "worktree-bridge")


@contextmanager
def _locked(base):
    _reject_links(base)
    base.mkdir(parents=True, exist_ok=True)
    lock = base / "registry.lock"
    deadline = time.monotonic() + 5
    while True:
        _reject_links(lock)
        try:
            descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise RuntimeError("Registration is locked; another init may be running. Inspect " + str(lock))
            time.sleep(0.02)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(str(os.getpid()))
        yield
    finally:
        lock.unlink()


def _atomic_json(path, value, *, replace):
    _reject_links(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".registration-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        _reject_links(path)
        if replace:
            os.replace(temporary, path)
        else:
            # Publish a complete file with an atomic no-overwrite operation.
            os.link(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _index(base):
    path = base / "registry.json"
    _reject_links(path)
    if not path.exists():
        return {"schema": 1, "projects": {}}
    index = _read_json(path, "project registry")
    if index.get("schema") != 1 or not isinstance(index.get("projects"), dict):
        raise RuntimeError("Invalid project registry; refusing to overwrite existing registrations")
    if any(not isinstance(value, dict)
           or any(not isinstance(value.get(key), str) for key in ("project_id", "local_root", "config_path", "name"))
           for value in index["projects"].values()):
        raise RuntimeError("Invalid project registration entry; refusing to overwrite existing registrations")
    return index


def find_config(cwd):
    """Find the nearest registered worktree without starting Git or SSH."""
    try:
        root, gitdir = _metadata(cwd)
        path = _config_path(gitdir)
        if not path.is_file():
            raise RuntimeError("This Git worktree is not registered; run 'wtb init' in it first")
        _validate_config(_read_json(path, "project configuration"), root)
        return path
    except OSError as exc:
        raise RuntimeError("Cannot discover the registered project: " + str(exc)) from exc


def init_project(cwd, remote=None, name=None, from_config=None):
    """Register an existing Git worktree, or confirm its unchanged binding."""
    try:
        return _init_project(cwd, remote, name, from_config)
    except OSError as exc:
        raise RuntimeError("Cannot register the Git worktree: " + str(exc)) from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Git did not confirm the current worktree within 15 seconds") from exc


def _init_project(cwd, remote, name, from_config):
    root, gitdir = _metadata(cwd)
    for option, expected in (("--show-toplevel", root), ("--absolute-git-dir", gitdir)):
        process = subprocess.run(["git", "-C", str(root), "rev-parse", option],
                                 env=git_environment(), capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=15,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if process.returncode:
            raise RuntimeError("Git did not confirm this worktree and its private metadata: " + process.stderr.strip())
        confirmed = _absolute(process.stdout.strip())
        _reject_links(confirmed)
        if confirmed.resolve() != expected:
            raise RuntimeError("Git did not confirm this worktree and its private metadata: " + process.stderr.strip())
    if name is not None and (not isinstance(name, str) or not name.strip() or any(ord(char) < 32 for char in name)):
        raise RuntimeError("Project name must be a non-empty string without control characters")
    if from_config is not None and remote is not None:
        raise RuntimeError("Use either remote or from_config, not both")
    path = _config_path(gitdir)
    identity = json.dumps([os.path.normcase(str(root)), os.path.normcase(str(gitdir))], separators=(",", ":"))
    project_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    base = _registry_root()
    config = None
    if from_config is not None:
        config = _read_json(_absolute(from_config), "migration configuration")
        if name is not None:
            config["name"] = name
        _validate_config(config, root)
    elif remote is not None:
        config = {"local_root": str(root), "remote": _remote(remote, root),
                  "state_dir": str(base / "projects" / project_id / "state")}
        if name is not None:
            config["name"] = name
        _validate_config(config, root)
    if config is not None:
        _outside_worktrees(base, root, config["remote"])
    elif not path.exists():
        raise RuntimeError("A remote binding or --from-config is required for the first registration")
    else:
        existing = _read_json(path, "project configuration")
        _validate_config(existing, root)
        _outside_worktrees(base, root, existing["remote"])
    with _locked(base):
        index = _index(base)
        previous = index["projects"].get(project_id)
        if previous is not None and (Path(previous["local_root"]) != root or Path(previous["config_path"]) != path):
            raise RuntimeError("Project identity collides with an existing registration; refusing to overwrite it")
        registered = not path.exists()
        if registered:
            _atomic_json(path, config, replace=False)
        else:
            existing = _read_json(path, "project configuration")
            _validate_config(existing, root)
            if from_config is not None and config != existing:
                raise RuntimeError("This worktree is already registered with a different configuration")
            if remote is not None:
                supplied_remote = dict(config["remote"])
                existing_remote = _remote(existing["remote"], root)
                if supplied_remote["kind"] == existing_remote["kind"] == "local":
                    supplied_remote["root"] = Path(supplied_remote["root"])
                    existing_remote["root"] = Path(existing_remote["root"])
                if supplied_remote != existing_remote:
                    raise RuntimeError("This worktree is already registered with a different remote binding")
            if name is not None and existing.get("name") != name:
                raise RuntimeError("This worktree is already registered with a different name")
            config = existing
        entry = previous or {
            "project_id": project_id, "local_root": str(root), "config_path": str(path),
            "name": str(config.get("name") or root.name), "registered_at": time.time(),
        }
        if previous is None:
            index["projects"][project_id] = entry
            _atomic_json(base / "registry.json", index, replace=True)
    return {"ok": True, "config_path": str(path), "project_id": project_id,
            "registered": registered, "local_root": str(root),
            "name": str(config.get("name") or root.name), "state_dir": config["state_dir"]}


def list_projects():
    """Read the central index without starting Git or contacting a peer."""
    try:
        return sorted(_index(_registry_root())["projects"].values(),
                      key=lambda entry: (entry.get("name", ""), entry.get("local_root", "")))
    except OSError as exc:
        raise RuntimeError("Cannot read registered projects: " + str(exc)) from exc
