import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from duolo import client


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({"local_root": str(self.root / "local"),
                                          "remote": {"kind": "local", "root": str(self.root / "remote")},
                                          "state_dir": str(self.root / "state")}), encoding="utf-8")
        _, self.state, self.fingerprint = client.configuration_info(self.config)
        self.state.mkdir()
        self.instance = "instance-1"
        self.received = []
        self.states = ["synced"]
        self.wrong_instance = False
        self.redirect = False
        self.complete_observe = True
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                if owner.redirect:
                    self.send_response(302)
                    self.send_header("Location", "http://example.invalid/")
                    self.end_headers()
                    return
                state = owner.states.pop(0) if len(owner.states) > 1 else owner.states[0]
                value = {"instance_id": "other" if owner.wrong_instance else owner.instance,
                         "config_fingerprint": owner.fingerprint, "state": state,
                         "recent_actions": ([{"id": "request-1", "status": "complete"}]
                                            if owner.received and owner.complete_observe else [])}
                self.reply(value)

            def do_POST(self):
                value = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.received.append((self.path, dict(self.headers), value))
                self.reply({"ok": True, "queued": True, "id": "request-1"})

            def reply(self, value):
                data = json.dumps(value).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.info = {"url": "http://127.0.0.1:" + str(self.server.server_port),
                     "pid": 123, "instance_id": self.instance, "config_fingerprint": self.fingerprint}
        self.save_info()

    def save_info(self):
        (self.state / "service.json").write_text(json.dumps(self.info), encoding="utf-8")

    def test_cached_status_does_not_construct_an_endpoint(self):
        with mock.patch("subprocess.run", side_effect=AssertionError("must not start Git or SSH")):
            self.assertEqual(client.get_status(self.config)["state"], "synced")

    def test_action_sends_same_origin_and_explicit_header(self):
        self.assertTrue(client.action(self.config, "pause")["queued"])
        path, headers, body = self.received[0]
        self.assertEqual(path, "/api/actions/pause")
        self.assertEqual(headers["X-Wtb-Local"], "1")
        self.assertEqual(headers["Origin"], self.info["url"])
        self.assertEqual(body, {})

    def test_port_reuse_or_configuration_change_cannot_receive_actions(self):
        self.wrong_instance = True
        with self.assertRaises(client.ServiceUnavailable):
            client.action(self.config, "checkpoint", {"message": "test"})
        self.assertEqual(self.received, [])

    def test_nonlocal_record_rejected_before_network(self):
        self.info["url"] = "http://example.invalid:1234"
        self.save_info()
        with mock.patch("urllib.request.OpenerDirector.open") as open_url:
            with self.assertRaises(client.ServiceUnavailable):
                client.get_status(self.config)
            open_url.assert_not_called()

    def test_redirect_is_not_followed(self):
        self.redirect = True
        with self.assertRaises(client.ClientError):
            client.get_status(self.config)

    def test_configuration_fingerprint_mismatch_rejected(self):
        self.info["config_fingerprint"] = "another-project"
        self.save_info()
        with self.assertRaises(client.ServiceUnavailable):
            client.get_status(self.config)

    def test_wait_reports_timeout_and_observed_state(self):
        self.states = ["conflict"]
        with self.assertRaises(client.WaitTimeout) as failure:
            client.wait_until_synced(self.config, timeout=0)
        self.assertEqual(failure.exception.last_status["state"], "conflict")

    def test_wait_returns_only_when_synced(self):
        self.states = ["syncing", "synced"]
        self.assertEqual(client.wait_until_synced(self.config, timeout=2)["state"], "synced")

    def test_old_green_cache_cannot_satisfy_new_wait(self):
        self.complete_observe = False
        with self.assertRaises(client.WaitTimeout):
            client.wait_until_synced(self.config, timeout=0)
        self.assertEqual(self.received[0][0], "/api/actions/observe")

    def test_stop_remains_available_after_configuration_edit(self):
        config = json.loads(self.config.read_text())
        config["name"] = "Changed project label"
        self.config.write_text(json.dumps(config), encoding="utf-8")
        with self.assertRaises(client.ServiceUnavailable):
            client.get_status(self.config)
        self.assertTrue(client.action(self.config, "stop")["queued"])

    def test_missing_service_and_unknown_action(self):
        with self.assertRaises(client.ClientError):
            client.action(self.config, "run-shell", {})
        (self.state / "service.json").unlink()
        with self.assertRaises(client.ServiceUnavailable):
            client.get_status(self.config)


if __name__ == "__main__":
    unittest.main()
