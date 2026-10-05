import json
import os
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx

from niji.cli import _cmd_connectors
from niji.config import save_mcp_servers
from niji.mcp import HttpMCPServer, MCPServer, connect_all


class FakeHTTPClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []
        self.closed = False

    def post(self, url, *, headers, json, timeout):
        self.calls.append((url, headers, json, timeout))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def delete(self, *args, **kwargs):
        return httpx.Response(200, request=httpx.Request("DELETE", "https://example.test"))

    def close(self):
        self.closed = True


def json_response(payload, *, headers=None):
    return httpx.Response(200, json=payload, headers=headers or {},
                          request=httpx.Request("POST", "https://example.test/mcp"))


class HttpMCPTests(unittest.TestCase):
    def test_local_stdio_client_tracks_credential_environment_values(self):
        client = MCPServer("local", {"command": "fake-server", "env": {
            "GITHUB_TOKEN": "secret-token", "API_KEY": "secret-key",
            "GITHUB_PAT": "secret-pat", "PAT": "short-pat", "SAFE_LABEL": "ordinary"}})
        self.assertEqual(client._secrets, ["secret-token", "secret-key", "secret-pat", "short-pat"])
        self.assertEqual(client._redact("echo secret-pat and short-pat"),
                         "echo [redacted] and [redacted]")
        with self.assertRaisesRegex(ValueError, "environment must be an object"):
            MCPServer("invalid", {"command": "fake-server", "env": ["not", "an object"]})

    def test_local_stdio_tool_results_and_errors_redact_environment_secrets(self):
        client = MCPServer("local", {"command": "fake-server", "env": {
            "GITHUB_TOKEN": "stdio-secret-value"}})
        client._request = MagicMock(return_value={"content": [
            {"type": "text", "text": "result includes stdio-secret-value"}]})
        result = client.call("read", {})
        self.assertEqual(result, "result includes [redacted]")
        client._request = MagicMock(side_effect=RuntimeError("server echoed stdio-secret-value"))
        with self.assertRaises(RuntimeError) as raised:
            client.call("read", {})
        self.assertNotIn("stdio-secret-value", str(raised.exception))

    def test_stdio_late_response_after_timeout_does_not_kill_reader(self):
        class ControlledOutput:
            def __init__(self):
                self.ready = {1: threading.Event(), 2: threading.Event()}
                self.index = 0

            def __iter__(self):
                return self

            def __next__(self):
                self.index += 1
                mid = self.index
                if mid not in self.ready:
                    raise StopIteration
                if not self.ready[mid].wait(2):
                    raise StopIteration
                return json.dumps({"jsonrpc": "2.0", "id": mid, "result": {"id": mid}}) + "\n"

        output = ControlledOutput()
        request_sent = threading.Event()
        cleanup_waiting = threading.Event()

        class Stdin:
            def write(self, payload):
                output.ready[json.loads(payload)["id"]].set()
                request_sent.set()
            def flush(self):
                pass

        class ObservedLock:
            def __init__(self):
                self.lock = threading.Lock()
            def __enter__(self):
                if (threading.current_thread().name == "first-request"
                        and request_sent.is_set()):
                    cleanup_waiting.set()
                self.lock.acquire()
                return self
            def __exit__(self, *exc):
                self.lock.release()

        class Pending(dict):
            def __init__(self):
                super().__init__()
                self.lookup_started = threading.Event()
                self.release_lookup = threading.Event()
                self.blocked = False

            def _lookup(self, key, present):
                if key == 1 and present and not self.blocked:
                    self.blocked = True
                    self.lookup_started.set()
                    self.release_lookup.wait(2)
                return present

            def __contains__(self, key):
                return self._lookup(key, super().__contains__(key))

            def get(self, key, default=None):
                present = self._lookup(key, super().__contains__(key))
                return super().get(key, default) if present else default

        client = MCPServer("local", {"command": "fake-server"})
        client.proc = type("Process", (), {"stdin": Stdin(), "stdout": output})()
        client._pending = Pending()
        client._pending_lock = ObservedLock()
        reader = threading.Thread(target=client._read_loop, name="mcp-reader", daemon=True)
        reader.start()

        first_result = {}
        def first_request():
            try:
                first_result["value"] = client._request("first", {}, timeout=0.05)
            except Exception as exc:
                first_result["error"] = exc

        first = threading.Thread(target=first_request, name="first-request")
        first.start()
        self.assertTrue(client._pending.lookup_started.wait(1))
        # The cleanup lock signals only after q.get timed out and the requester is
        # contending with the reader's pending lookup; no timing sleep is needed.
        self.assertTrue(cleanup_waiting.wait(1))
        client._pending.release_lookup.set()
        first.join(2)
        self.assertFalse(first.is_alive())
        self.assertIsInstance(first_result.get("error"), TimeoutError)

        second = client._request("second", {}, timeout=1)
        self.assertEqual(second, {"id": 2})
        reader.join(1)
        self.assertFalse(reader.is_alive())

    def test_stdio_response_delivery_after_timeout_cleanup_uses_captured_queue_safely(self):
        from queue import Queue as RealQueue

        class ControlledOutput:
            def __init__(self):
                self.ready = {1: threading.Event(), 2: threading.Event()}
                self.index = 0
            def __iter__(self):
                return self
            def __next__(self):
                self.index += 1
                mid = self.index
                if mid not in self.ready or not self.ready[mid].wait(2):
                    raise StopIteration
                return json.dumps({"jsonrpc": "2.0", "id": mid,
                                   "result": {"id": mid}}) + "\n"

        output = ControlledOutput()
        class Stdin:
            def write(self, payload):
                output.ready[json.loads(payload)["id"]].set()
            def flush(self):
                pass

        delivery_started = threading.Event()
        release_delivery = threading.Event()
        class DelayedQueue(RealQueue):
            def put(self, item, block=True, timeout=None):
                delivery_started.set()
                release_delivery.wait(2)
                return super().put(item, block=block, timeout=timeout)

        client = MCPServer("local", {"command": "fake-server"})
        client.proc = type("Process", (), {"stdin": Stdin(), "stdout": output})()
        reader = threading.Thread(target=client._read_loop, daemon=True)
        reader.start()
        real_factory = RealQueue
        queue_number = 0
        def queue_factory(*args, **kwargs):
            nonlocal queue_number
            queue_number += 1
            return DelayedQueue(*args, **kwargs) if queue_number == 1 else real_factory(*args, **kwargs)

        first_result = {}
        def first_request():
            try:
                first_result["value"] = client._request("first", {}, timeout=0.05)
            except Exception as exc:
                first_result["error"] = exc

        with patch("niji.mcp.queue.Queue", side_effect=queue_factory):
            first = threading.Thread(target=first_request)
            first.start()
            self.assertTrue(delivery_started.wait(1))
            first.join(1)
            self.assertFalse(first.is_alive())
            self.assertIsInstance(first_result.get("error"), TimeoutError)
            self.assertEqual(client._pending, {})
            release_delivery.set()
            second = client._request("second", {}, timeout=1)
        self.assertEqual(second, {"id": 2})
        reader.join(1)
        self.assertFalse(reader.is_alive())

    def test_stdio_tools_list_failure_is_reported_for_cleanup(self):
        client = MCPServer("local", {"command": "fake-server"})
        client.proc = MagicMock()

        def request(method, params, timeout=60):
            if method == "initialize":
                return {}
            raise TimeoutError("tools/list timed out")

        with (patch.object(client, "_request", side_effect=request),
              patch.object(client, "_notify"),
              patch("niji.mcp.subprocess.Popen", return_value=client.proc),
              patch("niji.mcp.threading.Thread.start")):
            with self.assertRaisesRegex(TimeoutError, "tools/list timed out"):
                client.start()

    def test_stdio_stop_terminates_and_reaps_a_stuck_child(self):
        from subprocess import TimeoutExpired
        proc = MagicMock()
        proc.poll.return_value = None
        proc.wait.side_effect = [TimeoutExpired("fake", 2), 0]
        client = MCPServer("local", {"command": "fake-server"})
        client.proc = proc
        client.stop()
        proc.terminate.assert_called_once()
        proc.kill.assert_called_once()
        self.assertEqual(proc.wait.call_count, 2)
        proc.stdin.close.assert_called_once()
        proc.stdout.close.assert_called_once()

    def test_connect_all_stops_clients_whose_startup_fails(self):
        class FailingClient:
            def __init__(self):
                self.stopped = False
            def start(self):
                raise RuntimeError("startup failed")
            def stop(self):
                self.stopped = True

        http_client, stdio_client = FailingClient(), FailingClient()
        with (patch("niji.mcp.HttpMCPServer", return_value=http_client),
              patch("niji.mcp.MCPServer", return_value=stdio_client),
              patch("builtins.print")):
            clients = connect_all({
                "http": {"transport": "http", "url": "https://example.test/mcp"},
                "stdio": {"transport": "stdio", "command": "fake-server"}})
        self.assertEqual(clients, [])
        self.assertTrue(http_client.stopped)
        self.assertTrue(stdio_client.stopped)

    def _client(self, payloads, headers=None):
        return FakeHTTPClient([json_response(p, headers=headers if i == 0 else None)
                               for i, p in enumerate(payloads)])

    def test_nango_handshake_discovers_tools_and_uses_documented_auth_headers(self):
        transport = self._client([
            {"jsonrpc": "2.0", "id": 1,
             "result": {"protocolVersion": "2024-11-05", "capabilities": {}}},
            {},
            {"jsonrpc": "2.0", "id": 3, "result": {"tools": [
                {"name": "search", "description": "Search items",
                 "inputSchema": {"type": "object", "properties": {}}}]}}],
            headers={"MCP-Session-Id": "session-abc"})
        config = {"transport": "http", "url": "https://api.nango.dev/proxy/v2/mcp",
                  "api_key": "nango-secret", "provider_config_key": "linear",
                  "connection_id": "conn-1"}
        with patch("niji.mcp.httpx.Client", return_value=transport):
            client = HttpMCPServer("nango_linear", config)
            client.start()
        self.assertEqual(len(client.tools), 1)
        init_url, headers, body, _ = transport.calls[0]
        self.assertEqual(init_url, config["url"])
        self.assertEqual(headers["Authorization"], "Bearer nango-secret")
        self.assertEqual(headers["Provider-Config-Key"], "linear")
        self.assertEqual(headers["Connection-Id"], "conn-1")
        self.assertEqual(body["method"], "initialize")
        self.assertEqual(transport.calls[1][1]["MCP-Session-Id"], "session-abc")
        self.assertEqual(transport.calls[2][1]["MCP-Protocol-Version"], "2024-11-05")
        self.assertEqual(client.to_openai_tools()[0]["function"]["name"],
                         "nango_linear__search")

    def test_tool_calls_return_text_and_propagate_args(self):
        transport = self._client([
            {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2024-11-05"}},
            {},
            {"jsonrpc": "2.0", "id": 3, "result": {"tools": []}},
            {"jsonrpc": "2.0", "id": 4, "result": {"content": [
                {"type": "text", "text": "Found 2 records"}]}}])
        with patch("niji.mcp.httpx.Client", return_value=transport):
            client = HttpMCPServer("nango", {"url": "https://api.nango.dev/proxy/v2/mcp"})
            client.start()
            result = client.call("search", {"query": "test"})
        self.assertEqual(result, "Found 2 records")
        self.assertEqual(transport.calls[3][2]["params"], {
            "name": "search", "arguments": {"query": "test"}})

    def test_server_sent_event_responses_are_parsed(self):
        sse = httpx.Response(200, headers={"content-type": "text/event-stream"},
                             text='event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2024-11-05"}}\n\n',
                             request=httpx.Request("POST", "https://example.test/mcp"))
        transport = FakeHTTPClient([sse, json_response({}),
                                    json_response({"jsonrpc": "2.0", "id": 3,
                                                  "result": {"tools": []}})])
        with patch("niji.mcp.httpx.Client", return_value=transport):
            client = HttpMCPServer("remote", {"url": "https://example.test/mcp"})
            client.start()
        self.assertEqual(client.tools, [])

    def test_auth_failure_is_diagnostic_and_does_not_echo_secrets(self):
        error = httpx.Response(401, text="rejected", request=httpx.Request(
            "POST", "https://example.test/mcp"))
        transport = FakeHTTPClient([error])
        with patch("niji.mcp.httpx.Client", return_value=transport):
            client = HttpMCPServer("private", {"url": "https://example.test/mcp",
                                                "api_key": "super-secret"})
            with self.assertRaisesRegex(RuntimeError, "HTTP 401") as raised:
                client.start()
        self.assertNotIn("super-secret", str(raised.exception))

    def test_env_references_resolve_without_saving_secret_in_config(self):
        transport = self._client([
            {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2024-11-05"}},
            {},
            {"jsonrpc": "2.0", "id": 3, "result": {"tools": []}}])
        with patch.dict(os.environ, {"NANGO_KEY_TEST": "env-secret"}), \
             patch("niji.mcp.httpx.Client", return_value=transport):
            client = HttpMCPServer("nango", {"url": "https://api.nango.dev/proxy/v2/mcp",
                                              "api_key": "${NANGO_KEY_TEST}"})
            client.start()
        self.assertEqual(transport.calls[0][1]["Authorization"], "Bearer env-secret")
        self.assertNotIn("env-secret", json.dumps({"api_key": "${NANGO_KEY_TEST}"}))

    def test_missing_env_reference_fails_without_printing_any_secret(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "missing environment variable"):
                HttpMCPServer("nango", {"url": "https://example.test/mcp",
                                         "api_key": "${NANGO_MISSING_TEST}"})

    def test_unsupported_or_embedded_credential_urls_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "absolute http\\(s\\)"):
            HttpMCPServer("remote", {"url": "file:///etc/passwd"})
        with self.assertRaisesRegex(ValueError, "require HTTPS"):
            HttpMCPServer("remote", {"url": "http://remote.example/mcp"})
        local = HttpMCPServer("local", {"url": "http://localhost:8765/mcp"})
        local.stop()
        with self.assertRaisesRegex(ValueError, "embedded credentials"):
            HttpMCPServer("remote", {"url": "https://user:password@example.test/mcp"})

    def test_guided_nango_setup_saves_config_without_echoing_api_key(self):
        saved = {}
        fake_stdin = MagicMock()
        fake_stdin.isatty.return_value = True
        with (
            patch("sys.stdin", fake_stdin),
            patch("builtins.input", side_effect=["linear", "connection-1", ""]),
            patch("getpass.getpass", return_value="nango-private-token"),
            patch("niji.cli.load_mcp_servers", return_value={}),
            patch("niji.cli.save_mcp_servers", side_effect=lambda servers: saved.update(servers)),
            patch("builtins.print") as output,
        ):
            _cmd_connectors(["add", "nango"])
        self.assertEqual(saved["nango_linear"]["api_key"], "nango-private-token")
        self.assertEqual(saved["nango_linear"]["provider_config_key"], "linear")
        self.assertEqual(saved["nango_linear"]["connection_id"], "connection-1")
        self.assertNotIn("nango-private-token", " ".join(str(call) for call in output.call_args_list))

    def test_mcp_configuration_is_written_with_private_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / ".niji"
            file = root / "mcp.json"
            with patch("niji.config.CONFIG_DIR", root), patch("niji.config.MCP_FILE", file):
                save_mcp_servers({"test": {"transport": "stdio", "command": "echo"}})
            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)
            self.assertEqual(json.loads(file.read_text())["servers"]["test"]["command"], "echo")

    def test_connect_all_keeps_existing_stdio_and_adds_http(self):
        transport = self._client([
            {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2024-11-05"}},
            {},
            {"jsonrpc": "2.0", "id": 3, "result": {"tools": []}}])
        with (
            patch("niji.mcp.httpx.Client", return_value=transport),
            patch("niji.mcp.MCPServer.start") as stdio_start,
            patch("builtins.print"),
        ):
            clients = connect_all({
                "remote": {"transport": "http", "url": "https://example.test/mcp"},
                "local": {"command": "unused", "args": []}})
        self.assertEqual(len(clients), 2)
        self.assertIsInstance(clients[0], HttpMCPServer)
        stdio_start.assert_called_once()


if __name__ == "__main__":
    unittest.main()
