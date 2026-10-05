"""MCP clients for trusted local stdio servers and authenticated HTTP endpoints.

The Nango cloud MCP proxy uses Streamable HTTP JSON-RPC at
https://api.nango.dev/proxy/v2/mcp. Configuration lives in ~/.niji/mcp.json.
"""
import json
import os
import queue
import re
import subprocess
import threading
from urllib.parse import urlsplit

import httpx

from . import __version__
from .safety import subprocess_environment


class MCPServer:
    """Newline-delimited JSON-RPC MCP client over a local subprocess."""
    def __init__(self, name: str, cfg: dict):
        self.name = name
        self.command = cfg["command"]
        self.args = cfg.get("args", [])
        self.env = cfg.get("env", {})
        if self.env is None:
            self.env = {}
        if not isinstance(self.env, dict):
            raise ValueError("MCP server environment must be an object")
        secret_name = re.compile(
            r"(?:API[_-]?KEY|ACCESS[_-]?KEY|PRIVATE[_-]?KEY|PROVIDER[_-]?CONFIG[_-]?KEY|"
            r"CONNECTION[_-]?ID|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH|"
            r"(?:^|[_-])PAT(?:$|[_-]))", re.IGNORECASE)
        self._secrets = [str(value) for key, value in self.env.items()
                         if secret_name.search(str(key)) and value is not None and str(value)]
        self.proc = None
        self.tools = []
        self._id = 0
        self._pending = {}
        self._pending_lock = threading.Lock()
        self._send_lock = threading.Lock()

    def start(self, timeout=20):
        env = subprocess_environment(self.env)
        self.proc = subprocess.Popen(
            [self.command, *self.args],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1, env=env,
        )
        threading.Thread(target=self._read_loop, daemon=True).start()
        self._request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "niji-agent", "version": __version__},
        }, timeout=timeout)
        self._notify("notifications/initialized", {})
        listing = self._request("tools/list", {}, timeout=timeout)
        if not isinstance(listing, dict) or not isinstance(listing.get("tools", []), list):
            raise RuntimeError(f"MCP server '{self.name}' returned an invalid tool list")
        self.tools = listing.get("tools", [])

    def stop(self):
        proc = self.proc
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2)
        except Exception:
            pass
        for stream in (proc.stdin, proc.stdout):
            try:
                if stream:
                    stream.close()
            except Exception:
                pass

    def _read_loop(self):
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except Exception:
                continue
            mid = msg.get("id")
            if mid is not None:
                # Keep the lookup and timeout cleanup atomic. A timed-out response may still
                # arrive; it must never kill the sole reader thread with a KeyError.
                with self._pending_lock:
                    pending = self._pending.get(mid)
                if pending is not None:
                    pending.put(msg)

    def _request(self, method, params, timeout=60):
        with self._send_lock:
            self._id += 1
            mid = self._id
            q = queue.Queue()
            with self._pending_lock:
                self._pending[mid] = q
            try:
                self.proc.stdin.write(json.dumps({
                    "jsonrpc": "2.0", "id": mid, "method": method, "params": params}) + "\n")
                self.proc.stdin.flush()
            except Exception:
                with self._pending_lock:
                    self._pending.pop(mid, None)
                raise
        try:
            resp = q.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError(f"MCP server '{self.name}' did not respond to {method}")
        finally:
            with self._pending_lock:
                self._pending.pop(mid, None)
        if "error" in resp:
            raise RuntimeError(f"MCP error from '{self.name}': {resp['error']}")
        return resp.get("result", {})

    def _notify(self, method, params):
        with self._send_lock:
            self.proc.stdin.write(json.dumps({
                "jsonrpc": "2.0", "method": method, "params": params}) + "\n")
            self.proc.stdin.flush()

    def _redact(self, value):
        text = str(value)
        for secret in sorted((item for item in self._secrets if item), key=len, reverse=True):
            if len(secret) >= 4:
                text = text.replace(secret, "[redacted]")
            else:
                pattern = r"(?<![A-Za-z0-9])" + re.escape(secret) + r"(?![A-Za-z0-9])"
                text = re.sub(pattern, "[redacted]", text)
        return text[:1200]

    def to_openai_tools(self):
        return _to_openai_tools(self.name, self.tools)

    def call(self, tool_name, args):
        try:
            result = self._request("tools/call", {
                "name": tool_name, "arguments": args}, timeout=180)
        except Exception as exc:
            raise RuntimeError(self._redact(exc)) from None
        return self._redact(_format_tool_result(result))


class HttpMCPServer:
    """MCP client over Streamable HTTP, including Nango's authenticated proxy."""
    def __init__(self, name: str, cfg: dict):
        self.name = name
        self.url = str(cfg.get("url") or "").strip()
        try:
            parsed_url = urlsplit(self.url)
            hostname = parsed_url.hostname or ""
        except ValueError:
            raise ValueError("HTTP MCP connector URL is invalid") from None
        if parsed_url.scheme not in ("http", "https") or not hostname:
            raise ValueError("HTTP MCP connector URL must be an absolute http(s) URL")
        if parsed_url.username is not None or parsed_url.password is not None:
            raise ValueError("HTTP MCP connector URL must not contain embedded credentials")
        if parsed_url.scheme == "http" and hostname.lower() not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError("HTTP MCP credentials require HTTPS except for loopback servers")
        self.cfg = cfg
        self.tools = []
        self._id = 0
        self._lock = threading.Lock()
        self._session_id = None
        self._protocol_version = "2024-11-05"
        self._secrets = []
        self._headers = self._build_headers()
        self._client = httpx.Client(timeout=httpx.Timeout(45.0, connect=15.0),
                                    follow_redirects=False)

    def _resolve_value(self, value, label):
        if value is None:
            return ""
        value = str(value)
        match = re.fullmatch(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", value)
        if match:
            env_name = match.group(1)
            value = os.environ.get(env_name, "")
            if not value:
                raise ValueError(f"missing environment variable {env_name} for {label}")
        return value

    def _build_headers(self):
        headers = {"Accept": "application/json, text/event-stream",
                   "Content-Type": "application/json",
                   "User-Agent": f"niji-agent/{__version__}"}
        for key, value in (self.cfg.get("headers") or {}).items():
            if not re.fullmatch(r"[A-Za-z0-9-]{1,80}", str(key)):
                raise ValueError("invalid HTTP MCP header name")
            resolved = self._resolve_value(value, f"header {key}")
            if resolved:
                headers[str(key)] = resolved
                self._secrets.append(resolved)

        # Nango's documented MCP proxy authentication headers.
        api_key = self._resolve_value(self.cfg.get("api_key"), "Nango API key")
        provider = self._resolve_value(
            self.cfg.get("provider_config_key") or self.cfg.get("integration_id"),
            "Nango provider-config key")
        connection = self._resolve_value(self.cfg.get("connection_id"),
                                         "Nango connection ID")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
            self._secrets.extend([api_key, f"Bearer {api_key}"])
        if provider:
            headers["Provider-Config-Key"] = provider
            self._secrets.append(provider)
        if connection:
            headers["Connection-Id"] = connection
            self._secrets.append(connection)
        return headers

    def _redact(self, value):
        text = str(value)
        for secret in sorted((item for item in self._secrets if item), key=len, reverse=True):
            if len(secret) >= 4:
                text = text.replace(secret, "[redacted]")
            else:
                pattern = r"(?<![A-Za-z0-9])" + re.escape(secret) + r"(?![A-Za-z0-9])"
                text = re.sub(pattern, "[redacted]", text)
        return text[:1200]

    def _request(self, method, params=None, *, notification=False, timeout=60):
        with self._lock:
            self._id += 1
            request_id = self._id
        body = {"jsonrpc": "2.0", "method": method}
        if not notification:
            body["id"] = request_id
        if params is not None:
            body["params"] = params
        headers = dict(self._headers)
        if self._session_id:
            headers["MCP-Session-Id"] = self._session_id
        if method != "initialize":
            headers["MCP-Protocol-Version"] = self._protocol_version
        try:
            response = self._client.post(self.url, headers=headers, json=body,
                                         timeout=timeout)
        except httpx.HTTPError as exc:
            raise RuntimeError(f"HTTP MCP '{self.name}' network error: {self._redact(exc)}") from None
        session_id = response.headers.get("MCP-Session-Id") or response.headers.get("mcp-session-id")
        if session_id:
            self._session_id = session_id
        if response.status_code >= 400:
            detail = f"HTTP {response.status_code}"
            if response.status_code in (401, 403):
                detail += " (authentication or connection permission rejected)"
            raise RuntimeError(f"HTTP MCP '{self.name}' {method} failed: {detail}")
        if notification or response.status_code in (202, 204) or not response.content:
            return {}
        content_type = response.headers.get("content-type", "").lower()
        try:
            if "text/event-stream" in content_type:
                messages = []
                for line in response.text.splitlines():
                    if line.startswith("data:"):
                        data = line[5:].strip()
                        if data and data != "[DONE]":
                            messages.append(json.loads(data))
                parsed = next((item for item in messages
                               if item.get("id") == request_id),
                              messages[-1] if messages else {})
            else:
                parsed = response.json()
        except (ValueError, json.JSONDecodeError):
            raise RuntimeError(f"HTTP MCP '{self.name}' returned an invalid JSON-RPC response") from None
        if not isinstance(parsed, dict):
            raise RuntimeError(f"HTTP MCP '{self.name}' returned an invalid JSON-RPC response")
        if "error" in parsed:
            error = parsed.get("error") or {}
            raise RuntimeError(self._redact(error.get("message") or "MCP request failed"))
        result = parsed.get("result", parsed)
        if not isinstance(result, dict):
            raise RuntimeError(f"HTTP MCP '{self.name}' returned an invalid result")
        return result

    def start(self, timeout=20):
        initialized = self._request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "niji-agent", "version": __version__},
        }, timeout=timeout)
        self._protocol_version = initialized.get("protocolVersion") or "2024-11-05"
        self._request("notifications/initialized", {}, notification=True,
                      timeout=timeout)
        listing = self._request("tools/list", {}, timeout=timeout)
        tools = listing.get("tools", [])
        if not isinstance(tools, list):
            raise RuntimeError(f"HTTP MCP '{self.name}' returned an invalid tool list")
        self.tools = tools

    def stop(self):
        try:
            if self._session_id:
                headers = dict(self._headers)
                headers["MCP-Session-Id"] = self._session_id
                headers["MCP-Protocol-Version"] = self._protocol_version
                self._client.delete(self.url, headers=headers, timeout=5)
        except Exception:
            pass
        try:
            self._client.close()
        except Exception:
            pass

    def to_openai_tools(self):
        return _to_openai_tools(self.name, self.tools)

    def call(self, tool_name, args):
        try:
            result = self._request("tools/call", {
                "name": tool_name, "arguments": args}, timeout=180)
        except Exception as exc:
            raise RuntimeError(self._redact(exc)) from None
        return self._redact(_format_tool_result(result))


def _to_openai_tools(server_name, tools):
    out = []
    for tool in tools:
        if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
            continue
        out.append({
            "type": "function",
            "function": {
                "name": f"{server_name}__{tool['name']}",
                "description": (tool.get("description") or "")[:1000],
                "parameters": tool.get("inputSchema") or {
                    "type": "object", "properties": {}},
            },
        })
    return out


def _format_tool_result(result):
    parts = []
    for item in result.get("content", []):
        if item.get("type") == "text":
            parts.append(item.get("text", ""))
        else:
            parts.append(json.dumps(item)[:2000])
    return "\n".join(parts) or json.dumps(result)[:2000]


def connect_all(servers_cfg: dict):
    """Start configured MCP servers; one failing connector never kills Niji."""
    clients = []
    for name, cfg in (servers_cfg or {}).items():
        client = None
        try:
            if not isinstance(cfg, dict):
                raise ValueError("connector configuration must be an object")
            transport = str(cfg.get("transport", "stdio")).lower()
            if transport in ("http", "streamable-http", "nango"):
                client = HttpMCPServer(name, cfg)
            elif transport == "stdio":
                client = MCPServer(name, cfg)
            else:
                raise ValueError(f"unsupported MCP transport '{transport}'")
            client.start()
            print(f"[niji] connector connected: {name} ({len(client.tools)} tools)")
            clients.append(client)
        except Exception as exc:
            if client is not None:
                try:
                    client.stop()
                except Exception:
                    pass
            # Never echo a possibly credential-bearing connector exception.
            print(f"[niji] connector '{name}' failed to start ({type(exc).__name__}); "
                  "check its private configuration and network access")
    return clients
