"""Private loopback control API for the CLI and MCP; no standalone frontend."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import socket
import threading
from urllib.parse import urlsplit

from .service import BridgeService, _save


class BridgeHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, service):
        if address[0] != "127.0.0.1":
            raise ValueError("HTTP service must bind 127.0.0.1")
        self.service = service
        super().__init__(address, BridgeHandler)
        self.url = "http://127.0.0.1:" + str(self.server_port)

    def get_request(self):
        connection, address = super().get_request()
        connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return connection, address


class BridgeHandler(BaseHTTPRequestHandler):
    server_version = "Duolo/0.4"

    def log_message(self, format, *args):
        pass

    def _json(self, status, result):
        payload = json.dumps(result, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(payload)

    def _error(self, status, code, message):
        self._json(status, {"ok": False, "error": {"code": code, "message": message}})

    def _local_request(self):
        host = self.headers.get_all("Host", [])
        expected = "127.0.0.1:" + str(self.server.server_port)
        if len(host) != 1 or host[0] != expected:
            self._error(403, "foreign_host", "Host must match this loopback service")
            return False
        origins = self.headers.get_all("Origin", [])
        if origins and (len(origins) != 1 or origins[0] != self.server.url):
            self._error(403, "foreign_origin", "Origin must match this loopback service")
            return False
        return True

    def do_GET(self):
        if not self._local_request():
            return
        path = urlsplit(self.path).path
        if path == "/api/status":
            self._json(200, self.server.service.view())
            return
        self._error(404, "not_found", "This is a private control API; use duo status or duo watch")

    def do_POST(self):
        if not self._local_request():
            return
        if self.headers.get_all("X-WTB-Local", []) != ["1"]:
            self._error(403, "local_header_required", "X-WTB-Local: 1 is required")
            return
        instances = self.headers.get_all("X-WTB-Instance", [])
        if instances and instances != [self.server.service.view().get("instance_id")]:
            self._error(409, "stale_instance", "Service instance changed; refresh status")
            return
        if self.headers.get("Transfer-Encoding") is not None:
            self._error(400, "invalid_body", "Transfer-Encoding is unsupported")
            return
        try:
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) != 1:
                raise ValueError("Content-Length is required")
            length = int(lengths[0])
            if not 0 <= length <= 16384:
                raise ValueError("Action body exceeds 16 KiB")
            if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                raise ValueError("Content-Type must be application/json")
            self.connection.settimeout(5)
            data = self.rfile.read(length)
            if len(data) != length:
                raise ValueError("incomplete action body")
            params = json.loads(data) if data else {}
            if not isinstance(params, dict):
                raise ValueError("action body must be an object")
        except (ValueError, OSError) as exc:
            self._error(400, "invalid_body", str(exc))
            return
        path = urlsplit(self.path).path
        prefix = "/api/actions/"
        if not path.startswith(prefix) or "/" in path[len(prefix):]:
            self._error(404, "not_found", "Action not found")
            return
        name = path[len(prefix):]
        result = self.server.service.action(name, params)
        self._json(202 if result["ok"] else 409 if result["error"]["code"] == "stale_revision" else 400, result)
        if name == "stop" and result["ok"]:
            threading.Thread(target=self.server.shutdown, daemon=True).start()


def run_server(config_path, host="127.0.0.1", port=0):
    service = BridgeService(config_path)
    if not port:
        port = service.options.get("port", 0)
    server = BridgeHTTPServer((host, port), service)
    # Claim exclusive controller before publishing ownership; scans start later.
    try:
        service._controller.acquire()
    except Exception:
        server.server_close()
        raise
    metadata = {"schema": 1, "pid": os.getpid(), "url": server.url,
                "startup_id": os.environ.get("WTB_STARTUP_ID"),
                "instance_id": service.instance_id, "config_fingerprint": service.config_fingerprint}
    metadata_path = service.state_dir / "service.json"
    try:
        _save(metadata_path, metadata)
        service._thread = threading.Thread(target=service._loop, name="worktree-bridge-controller", daemon=True)
        service._thread.start()
        server.serve_forever(poll_interval=.1)
    finally:
        service.stop()
        server.server_close()
        service.join(60)
        if service._thread is None:
            service._controller.release()
        try:
            if json.loads(metadata_path.read_text(encoding="utf-8")).get("instance_id") == service.instance_id:
                metadata_path.unlink()
        except (OSError, ValueError):
            pass
