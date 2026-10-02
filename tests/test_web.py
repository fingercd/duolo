import http.client
import json
import threading
import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from duolo.web import BridgeHTTPServer


class StubService:
    def view(self):
        return {"state": "initializing", "instance_id": "test"}

    def action(self, name, params):
        if name not in ("pause", "stop"):
            return {"ok": False, "error": {"code": "invalid_action", "message": "unknown action"}}
        return {"ok": True, "queued": True}


class WebTests(unittest.TestCase):
    def setUp(self):
        self.server = BridgeHTTPServer(("127.0.0.1", 0), StubService())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def request(self, method="GET", path="/api/status", body=None, headers=None):
        client = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        try:
            client.request(method, path, body=body, headers=headers or {})
            response = client.getresponse()
            payload = response.read()
            return response.status, payload
        finally:
            client.close()

    def test_cache_status_is_public_only_on_matching_loopback_host(self):
        status, body = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["state"], "initializing")
        self.assertEqual(self.request(headers={"Host": "evil.example"})[0], 403)
        self.assertEqual(self.request(headers={"Origin": "https://evil.example"})[0], 403)

    def test_action_requires_local_header_and_same_origin(self):
        headers = {"Content-Type": "application/json"}
        self.assertEqual(self.request("POST", "/api/actions/pause", "{}", headers)[0], 403)
        headers["X-WTB-Local"] = "1"
        headers["Origin"] = self.server.url
        status, body = self.request("POST", "/api/actions/pause", "{}", headers)
        self.assertEqual(status, 202)
        self.assertTrue(json.loads(body)["queued"])
        headers["Origin"] = "http://127.0.0.1:1"
        self.assertEqual(self.request("POST", "/api/actions/pause", "{}", headers)[0], 403)
        headers["Origin"] = self.server.url
        headers["X-WTB-Instance"] = "old-instance"
        self.assertEqual(self.request("POST", "/api/actions/pause", "{}", headers)[0], 409)

    def test_no_arbitrary_commands_or_static_path_escape(self):
        headers = {"Content-Type": "application/json", "X-WTB-Local": "1"}
        self.assertEqual(self.request("POST", "/api/actions/shell", "{}", headers)[0], 400)
        self.assertEqual(self.request(path="/%2e%2e/service.py")[0], 404)
        self.assertEqual(self.request(path="/config.json")[0], 404)
        self.assertEqual(self.request(path="/api/events")[0], 404)
        self.assertEqual(self.request(path="/")[0], 404)
        self.assertEqual(self.request(path="/index.html")[0], 404)

    def test_malformed_json_and_non_object_body(self):
        headers = {"Content-Type": "application/json", "X-WTB-Local": "1"}
        self.assertEqual(self.request("POST", "/api/actions/pause", "{", headers)[0], 400)
        self.assertEqual(self.request("POST", "/api/actions/pause", "[]", headers)[0], 400)
        self.assertEqual(self.request("POST", "/api/actions/pause", " " * 16385, headers)[0], 400)

    def test_external_bind_rejected(self):
        with self.assertRaisesRegex(ValueError, "127.0.0.1"):
            BridgeHTTPServer(("0.0.0.0", 0), StubService())


if __name__ == "__main__":
    unittest.main()
