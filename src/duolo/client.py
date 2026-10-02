"""Fast loopback client. It never scans a project or opens SSH."""

import hashlib
import json
from pathlib import Path
import time
from urllib import error, parse, request


class ClientError(RuntimeError):
    pass


class ServiceUnavailable(ClientError):
    pass


class WaitTimeout(ClientError):
    def __init__(self, message, last_status):
        super().__init__(message)
        self.last_status = last_status


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ClientError("The local service returned an unexpected redirect")


def configuration_info(config_path):
    try:
        config = json.loads(Path(config_path).read_text(encoding="utf-8"))
        state = Path(config["state_dir"])
        if not state.is_absolute():
            raise ValueError("state_dir must be absolute")
        roots = [Path(config["local_root"]).resolve()]
        if config["remote"]["kind"] == "local":
            roots.append(Path(config["remote"]["root"]).resolve())
        resolved_state = state.resolve()
        if any(resolved_state == root or root in resolved_state.parents for root in roots):
            raise ValueError("state_dir must be outside the worktrees")
        canonical = json.dumps(config, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ClientError("Cannot read the project configuration: " + str(exc)) from exc
    return config, state, hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def service_info(config_path, *, allow_changed_config=False):
    _, state, fingerprint = configuration_info(config_path)
    try:
        info = json.loads((state / "service.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ServiceUnavailable("Service is not running; use 'duo start' first") from exc
    try:
        url = parse.urlsplit(info["url"])
        valid = (url.scheme == "http" and url.hostname == "127.0.0.1"
                 and url.port is not None and 1 <= url.port <= 65535
                 and not url.username and not url.password and not url.query
                 and not url.fragment and url.path in ("", "/"))
        if not valid:
            raise ValueError("only an exact loopback HTTP endpoint is allowed")
        if (not allow_changed_config and info["config_fingerprint"] != fingerprint) or not info["instance_id"]:
            raise ValueError("service belongs to a different configuration")
    except (ValueError, TypeError, KeyError) as exc:
        raise ServiceUnavailable("Invalid service record: " + str(exc)) from exc
    return info


def _request(info, path, payload=None, timeout=2):
    url = info["url"].rstrip("/") + path
    headers = {"Accept": "application/json", "X-WTB-Instance": info["instance_id"]}
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers.update({"Content-Type": "application/json", "X-WTB-Local": "1",
                        "Origin": info["url"].rstrip("/")})
    req = request.Request(url, data=data, headers=headers)
    # Ignore HTTP_PROXY/HTTPS_PROXY: a local control request must remain local.
    opener = request.build_opener(request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(req, timeout=timeout) as response:
            body = response.read(2 * 1024 * 1024 + 1)
        if len(body) > 2 * 1024 * 1024:
            raise ClientError("Local service response is too large")
        result = json.loads(body)
    except error.HTTPError as exc:
        detail = exc.read(4096).decode("utf-8", "replace")
        raise ClientError(f"Local service rejected the request ({exc.code}): {detail}") from exc
    except (error.URLError, TimeoutError, ConnectionError, OSError) as exc:
        raise ServiceUnavailable("Local service is unavailable: " + str(exc)) from exc
    except (ValueError, UnicodeError) as exc:
        raise ClientError("Local service returned invalid JSON") from exc
    if not isinstance(result, dict):
        raise ClientError("Local service returned a non-object response")
    return result


def _checked_status(info, timeout=2):
    status = _request(info, "/api/status", timeout=timeout)
    if (status.get("instance_id") != info["instance_id"]
            or status.get("config_fingerprint") != info["config_fingerprint"]):
        raise ServiceUnavailable("Service identity changed; refusing to control another instance")
    return status


def get_status(config_path):
    """Return cached state promptly, even while the worker is busy or offline."""
    return _checked_status(service_info(config_path))


def action(config_path, name, params=None):
    if name not in {"sync", "observe", "pause", "resume", "resolve", "checkpoint", "stop"}:
        raise ClientError("Unknown service action")
    info = service_info(config_path, allow_changed_config=name == "stop")
    _checked_status(info)
    result = _request(info, "/api/actions/" + name, params or {})
    if result.get("ok") is False:
        detail = result.get("error", "Action rejected")
        if isinstance(detail, dict):
            detail = detail.get("message", str(detail))
        raise ClientError(str(detail))
    return result


def wait_until_synced(config_path, timeout=30):
    if not isinstance(timeout, (int, float)) or not 0 <= timeout <= 3600:
        raise ClientError("timeout must be between 0 and 3600 seconds")
    deadline = time.monotonic() + timeout
    # A green cache from before the caller's save cannot satisfy this wait.
    # Observe is a read barrier, not a manual sync overriding pause/auto_sync.
    receipt = action(config_path, "observe", {})
    identifier = receipt.get("id")
    if not identifier:
        raise ClientError("Service did not return an observation receipt")
    while True:
        status = get_status(config_path)
        observation = next((item for item in status.get("recent_actions", [])
                            if item.get("id") == identifier), None)
        if observation and observation.get("status") == "failed":
            raise ClientError("Observation failed: " + str(observation.get("error")))
        if observation and observation.get("status") == "complete" and status.get("state") == "synced":
            return status
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise WaitTimeout("Project has not reached a confirmed synchronized state", status)
        time.sleep(min(0.2, remaining))
