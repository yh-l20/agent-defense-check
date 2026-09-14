"""Synthetic pilot integration verification for MetaOrdo gateway and Yuanxingmu HostModel.

This test suite provides an independent, reproducible verification harness for
the 4 pilot criteria agreed upon in Issue #6. It exercises actual HostModel,
ModelStore, Broker, and HTTP upstream interactions with zero production keys
and zero real models.
"""
from __future__ import annotations

from contextlib import ExitStack
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import queue
import secrets
import socket
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch


class _MockGatewayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self.server.request_count += 1
        self.server.receipts.put((self.command, self.path, dict(self.headers), body))

        resp_body = json.dumps({
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "synthetic answer"}}]
        }).encode()
        self.send_response_only(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp_body)))
        self.send_header("Connection", "close")
        corr = self.headers.get("X-YXM-Audit-Correlation-ID")
        if corr:
            self.send_header("X-YXM-Audit-Correlation-ID", corr)
        self.end_headers()
        self.wfile.write(resp_body)


class _CountingGateway(ThreadingHTTPServer):
    def get_request(self):
        accepted = super().get_request()
        self.tcp_accepts += 1
        return accepted


class _UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("unused-host", timeout=5)
        self.path = str(path)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


@unittest.skipUnless(sys.platform.startswith("linux"), "HostModel Unix socket requires Linux")
class MetaordoPilotIntegrationTests(unittest.TestCase):
    def setUp(self):
        from yuanxingmu.broker import Broker
        from yuanxingmu.gateway_network import HostModel
        from yuanxingmu.sdk_model_store import ModelStore
        from yuanxingmu.sdk_runtime import _SdkOutputGuard

        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="metaordo-pilot-")))
        self.broker = self.stack.enter_context(Broker(self.root / "authority", {}, {}))
        self.broker.create_task(task_id="pilot-task")
        self.store = ModelStore(self.root / "journal", "pilot-session")
        self.journal_path = self.store.directory / "state.json"
        self.begin = self.stack.enter_context(patch.object(self.store, "begin", wraps=self.store.begin))

        self.runtime = self.root / "runtime"
        self.runtime.mkdir(mode=0o700)

        # Mock Upstream Gateway
        self.gateway = _CountingGateway(("127.0.0.1", 0), _MockGatewayHandler)
        self.gateway.receipts = queue.Queue()
        self.gateway.tcp_accepts = 0
        self.gateway.request_count = 0
        self.gateway_thread = threading.Thread(target=self.gateway.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.gateway_thread.start()
        self.stack.callback(self.gateway_thread.join, 2)
        self.stack.callback(self.gateway.server_close)
        self.stack.callback(self.gateway.shutdown)

        # Separate connect attempts, server-side accepts, and HTTP requests.
        # Only this fixture's Unix sockets and loopback gateway may be reached.
        original_socket_connect = socket.socket.connect
        def fixture_only(connection, address):
            if connection.family == socket.AF_UNIX:
                if not str(address).startswith(str(self.root) + "/"):
                    raise AssertionError("unexpected Unix socket")
            elif connection.family != socket.AF_INET or tuple(address[:2]) != ("127.0.0.1", self.gateway.server_port):
                raise AssertionError("pilot tests forbid non-fixture network connections")
            return original_socket_connect(connection, address)
        self.stack.enter_context(patch.object(socket.socket, "connect", fixture_only))
        self.tcp_connect_attempts = 0
        original_connect = http.client.HTTPConnection.connect
        def tracked_connect(connection):
            self.tcp_connect_attempts += 1
            return original_connect(connection)
        self.stack.enter_context(patch.object(http.client.HTTPConnection, "connect", tracked_connect))

        self.body = json.dumps({
            "model": "pilot-model",
            "max_tokens": 512,
            "messages": [{"role": "user", "content": "synthetic test"}]
        }).encode()

        self.valid_corr = "corr-" + secrets.token_hex(16)
        self.metadata = lambda: {
            "X-YXM-Audit-Correlation-ID": self.valid_corr,
            "X-YXM-Audit-Protocol-Version": "1.0",
        }
        self.provider = Mock(side_effect=lambda: self.metadata())

        self.host_model = self.stack.enter_context(HostModel(
            self.runtime,
            model_url=f"http://127.0.0.1:{self.gateway.server_port}",
            api_key="sk-pilot-test-key",
            model_store=self.store,
            output_guard=_SdkOutputGuard(self.broker, "pilot-task", "pilot-model", 2048),
            audit_metadata_provider=self.provider
        ))

    def _request(self, path="/v1/chat/completions", headers=None):
        conn = _UnixHTTP(self.runtime / "model.sock")
        try:
            conn.request("POST", path, body=self.body, headers=headers or {})
            resp = conn.getresponse()
            return resp.status, dict(resp.getheaders()), resp.read()
        finally:
            conn.close()

    def _assert_no_upstream_or_journal_change(self, journal_before):
        self.assertEqual(self.tcp_connect_attempts, 0)
        self.assertEqual(self.gateway.tcp_accepts, 0)
        self.assertEqual(self.gateway.request_count, 0)
        self.assertTrue(self.gateway.receipts.empty())
        self.begin.assert_not_called()
        self.assertEqual(self.journal_path.read_bytes(), journal_before)

    def test_criterion1_and_2_correlation_penetration_and_unidirectional_isolation(self):
        status, worker_resp_headers, data = self._request(
            headers={
                "X-Worker-Spoofed": "injected-by-worker",
                "X-YXM-Audit-Correlation-ID": "forged-corr-must-be-stripped",
                "Authorization": "Bearer worker-fake-token",
            }
        )
        self.assertEqual(status, 200)

        cmd, path, gateway_headers, body = self.gateway.receipts.get(timeout=2)
        # 1. Gateway preserved valid correlation and protocol version
        self.assertEqual(gateway_headers["X-YXM-Audit-Correlation-ID"], self.valid_corr)
        self.assertRegex(gateway_headers["X-YXM-Audit-Correlation-ID"], r"^corr-[0-9a-f]{32}$")
        self.assertEqual(gateway_headers["X-YXM-Audit-Protocol-Version"], "1.0")
        self.assertEqual(gateway_headers["Authorization"], "Bearer sk-pilot-test-key")

        # 2. Worker spoofed header was stripped before reaching gateway
        self.assertNotIn("x-worker-spoofed", {k.lower() for k in gateway_headers})
        self.assertEqual(self.tcp_connect_attempts, 1)
        self.assertEqual(self.gateway.tcp_accepts, 1)
        self.assertEqual(self.gateway.request_count, 1)

        # 3. HostModel stripped gateway's echoed correlation ID, preventing echo to worker
        self.assertNotIn("x-yxm-audit-correlation-id", {k.lower() for k in worker_resp_headers})

        # 4. Store journal records complete status
        records = json.loads(self.journal_path.read_bytes())["records"]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["state"], "complete")

    def test_criterion3_invalid_metadata_zero_upstream_and_no_ticket(self):
        journal_before = self.journal_path.read_bytes()
        self.metadata = lambda: {"X-YXM-Audit-Correlation-ID": "corr-bad!format",
                                 "X-YXM-Audit-Protocol-Version": "1.0"}
        status, _, _ = self._request()
        self.assertEqual(status, 500)
        self.provider.assert_called_once()
        self._assert_no_upstream_or_journal_change(journal_before)

    def test_criterion3_revocation_before_provider_zero_upstream(self):
        journal_before = self.journal_path.read_bytes()
        self.broker.revoke("pilot-task")
        status, _, data = self._request()
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)["model"], "yuanxingmu-host")
        self.provider.assert_not_called()
        self._assert_no_upstream_or_journal_change(journal_before)

    def test_criterion3_revocation_during_provider_zero_upstream(self):
        # Independent setUp leaves this task live until the callback runs.
        journal_before = self.journal_path.read_bytes()
        valid = self.metadata
        def revoke_then_return():
            self.broker.revoke("pilot-task")
            return valid()

        self.metadata = revoke_then_return
        status, _, data = self._request()
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)["model"], "yuanxingmu-host")
        self.provider.assert_called_once()
        self._assert_no_upstream_or_journal_change(journal_before)

    def test_criterion4_explicit_replay_exact_bytes_zero_upstream_and_no_new_ticket(self):
        # 1. First initial request
        first_status, first_headers, first_body = self._request()
        self.assertEqual(first_status, 200)
        self.gateway.receipts.get(timeout=2)
        self.assertEqual(self.tcp_connect_attempts, 1)
        self.assertEqual(self.gateway.tcp_accepts, 1)
        self.assertEqual(self.gateway.request_count, 1)
        self.provider.assert_called_once()
        self.begin.assert_called_once_with(self.body)

        journal_after_first = self.journal_path.read_bytes()

        # Reset counters & mocks
        self.tcp_connect_attempts = 0
        self.gateway.tcp_accepts = 0
        self.gateway.request_count = 0
        self.provider.reset_mock()
        self.begin.reset_mock()
        self.provider.side_effect = AssertionError("Replay must never invoke audit metadata provider")

        # 2. Explicit /v1/chat/completions/replay request
        replay_status, replay_headers, replay_body = self._request(path="/v1/chat/completions/replay")

        # 3. Assert exact byte-for-byte identity
        self.assertEqual(replay_status, first_status)
        self.assertEqual(replay_body, first_body)

        # 4. Assert zero upstream TCP connections & zero HTTP requests
        self.assertEqual(self.tcp_connect_attempts, 0)
        self.assertEqual(self.gateway.tcp_accepts, 0)
        self.assertEqual(self.gateway.request_count, 0)

        # 5. Assert provider not called, no new ticket created, and journal untouched
        self.provider.assert_not_called()
        self._assert_no_upstream_or_journal_change(journal_after_first)


if __name__ == "__main__":
    unittest.main()
