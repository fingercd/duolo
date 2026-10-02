"""One persistent JSONL Python process per fixed local or SSH endpoint."""
import base64
from collections import deque
import json
from pathlib import Path
import queue
import shlex
import subprocess
import sys
import threading
import zlib


# Read the source bundle through stdin: even a large bundle never exceeds the
# Windows command-line limit and no source file is installed on the SSH host.
BOOTSTRAP = r'''
import base64,json,sys,types,zlib
bundle=json.loads(zlib.decompress(base64.b64decode(sys.stdin.buffer.readline())))
package=types.ModuleType("worktree_bridge")
package.__path__=[]
sys.modules["worktree_bridge"]=package
for name,source in bundle["modules"]:
    full="worktree_bridge."+name
    module=types.ModuleType(full)
    module.__package__="worktree_bridge"
    module.__file__="<bundled:"+name+">"
    sys.modules[full]=module
    setattr(package,name,module)
    exec(compile(source,module.__file__,"exec"),module.__dict__)
package.runtime_agent.serve(bundle["root"],bundle["reconcile_interval"])
'''


class PeerError(RuntimeError):
    """A peer operation or connection failed; writes must be checked before retry."""


class Peer:
    def __init__(self, spec, timeout=45, reconcile_interval=30):
        from .__main__ import Endpoint
        endpoint = Endpoint(spec)
        self.spec = dict(spec)
        self.root = endpoint.root
        self.timeout = timeout
        self.ssh_identity = getattr(endpoint, "ssh_identity", None)
        self._lock = threading.Lock()
        self._responses = queue.Queue()
        self._outgoing = queue.Queue()
        self._stderr = deque(maxlen=64)
        self._closed = False
        self._sequence = 0
        directory = Path(__file__).parent
        modules = []
        for name in ("agent", "watching", "runtime_agent", "git_ops"):
            path = directory / (name + ".py")
            if path.exists():
                modules.append((name, path.read_text(encoding="utf-8-sig")))
        bundle = {"modules": modules, "root": self.root, "reconcile_interval": reconcile_interval}
        encoded = base64.b64encode(zlib.compress(json.dumps(bundle, ensure_ascii=True).encode("utf-8"))) + b"\n"
        if spec["kind"] == "local":
            command = [sys.executable, "-u", "-c", BOOTSTRAP]
        else:
            command = [*endpoint.ssh_args, "python3 -u -c " + shlex.quote(BOOTSTRAP)]
        process_options = {}
        if sys.platform == "win32":
            process_options["creationflags"] = subprocess.CREATE_NO_WINDOW
        self._process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, bufsize=65536, **process_options)
        self._readers = []
        for target in (self._read_responses, self._read_stderr, self._write_requests):
            thread = threading.Thread(target=target, daemon=True)
            thread.start()
            self._readers.append(thread)
        self._outgoing.put(encoded)

    def _write_requests(self):
        try:
            while True:
                data = self._outgoing.get()
                if data is None:
                    return
                remaining = memoryview(data)
                while remaining:
                    count = self._process.stdin.write(remaining)
                    if count is None or count <= 0:
                        raise OSError("peer stdin closed during request")
                    remaining = remaining[count:]
                self._process.stdin.flush()
        except (OSError, ValueError) as exc:
            self._responses.put(PeerError("peer connection write failed: " + str(exc)))

    def _read_responses(self):
        try:
            for line in self._process.stdout:
                try:
                    response = json.loads(line)
                except (ValueError, UnicodeError) as exc:
                    self._responses.put(PeerError("peer returned invalid JSONL: " + str(exc)))
                    return
                self._responses.put(response)
        except (OSError, ValueError) as exc:
            self._responses.put(PeerError("peer connection read failed: " + str(exc)))
        finally:
            self._responses.put(PeerError("peer disconnected; operation outcome may be unknown"))

    def _read_stderr(self):
        try:
            # Bounded chunks avoid unbounded accumulation from a missing newline.
            while True:
                chunk = self._process.stderr.read(1024)
                if not chunk:
                    return
                self._stderr.append(chunk.decode("utf-8", "replace"))
        except (OSError, ValueError):
            return

    def _failure(self, message):
        self.close()
        detail = "".join(self._stderr).strip()
        return PeerError(message + ((": " + detail) if detail else ""))

    def call(self, op, **payload):
        with self._lock:
            if self._closed:
                raise PeerError("peer is closed; create a new peer and inspect actual state")
            if any(key in payload for key in ("id", "root", "op")):
                raise ValueError("peer id, operation and root are fixed")
            self._sequence += 1
            request_id = self._sequence
            request = {"id": request_id, "root": self.root, "op": op, **payload}
            try:
                self._outgoing.put(json.dumps(request, ensure_ascii=True).encode("utf-8") + b"\n")
                response = self._responses.get(timeout=self.timeout)
            except queue.Empty as exc:
                raise self._failure("peer request timed out; do not replay writes before inspecting actual state") from exc
            except (OSError, ValueError) as exc:
                raise self._failure("peer connection failed; operation outcome may be unknown") from exc
            if isinstance(response, Exception):
                raise self._failure(str(response))
            if not isinstance(response, dict) or response.get("id") != request_id or type(response.get("ok")) is not bool:
                raise self._failure("peer returned an invalid response or request id")
            if not response["ok"]:
                raise PeerError("peer operation " + op + " failed: " + str(response.get("error")))
            if "result" not in response:
                raise self._failure("peer response is missing result")
            return response["result"]

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._outgoing.put(None)
        process = self._process
        # Closing stdin can block while a request writer is stuck. Terminate first;
        # the process must never receive a replay after a transport timeout.
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        for stream in (process.stdin, process.stdout, process.stderr):
            try:
                stream.close()
            except OSError:
                # A terminated writer may still have bytes buffered. Cleanup
                # must preserve the original timeout/disconnection error.
                pass
        for thread in self._readers:
            if thread is not threading.current_thread():
                thread.join(timeout=0.3)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()
