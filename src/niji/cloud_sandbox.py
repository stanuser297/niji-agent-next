"""Optional E2B-backed workspace for cloud coding runs.

The model loop and provider credential stay in the trusted worker. Only bounded
workspace operations are bridged to a fresh, network-disabled E2B sandbox.
"""
from __future__ import annotations

import math
import mimetypes
import os
import shlex
import threading
import time
from pathlib import Path, PurePosixPath
from typing import Any

from .cloud_runtime import RunRequest, validate_cloud_payload
from .cloud_worker import CloudRunContext, WorkerSettings, _agent_usage

_SANDBOX_ROOT = "/tmp/niji-workspace"
_MAX_SANDBOX_SECONDS = 3_600  # Works on E2B Hobby as well as Pro accounts.
_MAX_COMMAND_SECONDS = 120
_MAX_COMMAND_LENGTH = 10_000
_MAX_TOOL_OUTPUT = 20_000
_MAX_FILE_BYTES = 1_000_000
_MAX_ARTIFACTS_PER_RUN = 50
_MAX_ARTIFACT_TOTAL_BYTES = 5_000_000
_MAX_ARTIFACT_SCAN_ENTRIES = 500
_BLOCKED_ARTIFACT_DIRS = {".git", ".niji", ".venv", "venv", "node_modules", "__pycache__"}
_BLOCKED_ARTIFACT_NAMES = {".env", "credentials.json", "secrets.json", "id_rsa", "id_ed25519"}
_ALLOWED_TOOLS = ("bash", "read_file", "write_file", "edit_file", "list_files")


def _safe_artifact_path(value: str) -> str | None:
    if (not isinstance(value, str) or not value or len(value) > 240
            or chr(0) in value or chr(92) in value or value.startswith("/")):
        return None
    path = PurePosixPath(value)
    if not path.parts or any(part in ("", ".", "..") for part in path.parts):
        return None
    names = [part.casefold() for part in path.parts]
    basename = names[-1]
    if (any(name in _BLOCKED_ARTIFACT_DIRS for name in names)
            or basename in _BLOCKED_ARTIFACT_NAMES
            or (basename.startswith(".env") and basename != ".env.example")
            or basename.endswith((".pem", ".key", ".p12", ".pfx"))):
        return None
    return path.as_posix()


class E2BSandboxExecutor:
    """Run Niji's model loop in the worker with workspace tools in E2B.

    Each run gets a new sandbox with outbound internet disabled and no worker
    environment variables forwarded into it. Optional initial project files are
    bounded plain-text entries validated by the API and shared payload validator.
    """

    def __init__(self, settings: WorkerSettings):
        self.settings = settings
        self._activity_callback = None
        self._artifact_callback = None
        self.usage: dict[str, Any] | None = None

    def set_activity_callback(self, callback):
        self._activity_callback = callback

    def set_artifact_callback(self, callback):
        self._artifact_callback = callback

    def __call__(self, request: RunRequest, context: CloudRunContext) -> dict[str, str]:
        prompt, project_files = validate_cloud_payload(request.payload)
        if request.timeout_seconds > _MAX_SANDBOX_SECONDS:
            raise ValueError("E2B runs are limited to 3600 seconds")
        context.check_cancelled()
        if "repository" in request.payload:
            from .cloud_repository import fetch_github_repository
            project_files = fetch_github_repository(
                request.payload["repository"], check_cancelled=context.check_cancelled
            )
        try:
            from e2b import Sandbox
        except ImportError as exc:  # pragma: no cover - depends on optional install
            raise RuntimeError("Install the pinned E2B cloud sandbox dependency") from exc
        if not os.environ.get("E2B_API_KEY"):
            raise ValueError("E2B_API_KEY is required in the trusted worker environment")

        # E2B SDK reads E2B_API_KEY in the worker. Never pass it, provider keys,
        # database URLs, or any other worker environment values as sandbox envs.
        sandbox = None
        agent = None
        lifecycle_lock = threading.Lock()
        killed = False

        def kill_once() -> None:
            nonlocal killed
            with lifecycle_lock:
                target = sandbox
                if killed or target is None:
                    return
                killed = True
            try:
                target.kill()
            except Exception:
                # Do not log or return provider diagnostics that may contain secrets.
                pass

        def cancel_run() -> None:
            if agent is not None:
                agent.cancel()
            kill_once()

        context.set_cancel_callback(cancel_run)
        with __import__("tempfile").TemporaryDirectory(prefix="niji-cloud-sandbox-") as local_workspace:
            tools = _RemoteWorkspaceTools(None, Path(local_workspace))
            try:
                remaining = context.deadline - time.monotonic()
                if remaining < 10:
                    context.check_cancelled()
                    raise TimeoutError("Insufficient time remains to start sandbox")
                sandbox = Sandbox.create(
                    timeout=max(10, min(_MAX_SANDBOX_SECONDS, math.ceil(remaining))),
                    allow_internet_access=False,
                    metadata={"service": "niji-cloud"},
                )
                tools.sandbox = sandbox
                if context.cancellation_requested:
                    kill_once()
                    context.check_cancelled()
                setup = sandbox.commands.run(
                    f"mkdir -p {shlex.quote(_SANDBOX_ROOT)}", cwd="/", timeout=10
                )
                if getattr(setup, "exit_code", 0) != 0:
                    raise RuntimeError("Sandbox workspace setup failed")
                for item in project_files:
                    context.check_cancelled()
                    tools.write_initial_file(item["path"], item["content"])
                context.check_cancelled()
                from .agent import Agent
                agent = Agent(
                    {
                        "provider": self.settings.provider,
                        "base_url": self.settings.base_url,
                        "model": self.settings.model,
                        "api_key": self.settings.provider_api_key,
                    },
                    approval="auto",
                    max_turns=self.settings.max_turns,
                    max_tool_calls=30,
                    max_tool_calls_per_turn=5,
                    mcp_clients=[],
                    allowed_tools=list(_ALLOWED_TOOLS),
                    workspace=local_workspace,
                    cloud_mode=True,
                    tool_dispatcher=tools.dispatch,
                    verbose=False,
                    cloud_prompt_token_limit=self.settings.run_prompt_token_cap,
                    cloud_completion_token_limit=self.settings.run_completion_token_cap,
                )
                agent.activity_callback = self._activity_callback

                context.check_cancelled()
                result = agent.chat(prompt)
                context.check_cancelled()
                if not isinstance(result, str):
                    raise ValueError("Agent returned a non-text response")
                encoded = result.encode("utf-8")
                if len(encoded) > 240_000:
                    result = encoded[:240_000].decode("utf-8", errors="ignore")
                if callable(self._artifact_callback):
                    self._collect_artifacts(sandbox, context)
                return {"text": result}
            finally:
                if agent is not None:
                    self.usage = _agent_usage(agent, self.settings)
                kill_once()

    def _collect_artifacts(self, sandbox: Any, context: CloudRunContext) -> None:
        entries = sandbox.files.list(_SANDBOX_ROOT, depth=20)
        total = 0
        count = 0
        for entry in entries[:_MAX_ARTIFACT_SCAN_ENTRIES]:
            context.check_cancelled()
            kind = getattr(getattr(entry, "type", None), "value", getattr(entry, "type", None))
            if kind != "file" or getattr(entry, "symlink_target", None):
                continue
            remote_path = getattr(entry, "path", "")
            if not isinstance(remote_path, str) or not remote_path.startswith(_SANDBOX_ROOT + "/"):
                continue
            relative = remote_path[len(_SANDBOX_ROOT) + 1:]
            safe_path = _safe_artifact_path(relative)
            if safe_path is None:
                continue
            try:
                hinted_size = int(getattr(entry, "size", 0))
            except (TypeError, ValueError):
                continue
            if hinted_size < 0 or hinted_size > _MAX_FILE_BYTES:
                continue
            raw = sandbox.files.read(remote_path, format="bytes")
            content = bytes(raw)
            if len(content) > _MAX_FILE_BYTES or total + len(content) > _MAX_ARTIFACT_TOTAL_BYTES:
                continue
            if count >= _MAX_ARTIFACTS_PER_RUN:
                break
            content_type = mimetypes.guess_type(safe_path)[0] or "application/octet-stream"
            self._artifact_callback(safe_path, content, content_type)
            count += 1
            total += len(content)


class _RemoteWorkspaceTools:
    """Small allowlisted bridge; no local host tool handler is called."""

    def __init__(self, sandbox: Any, local_workspace: Path):
        self.sandbox = sandbox
        self.local_workspace = local_workspace.resolve()
        self._lock = threading.RLock()

    def write_initial_file(self, relative_path: str, content: str) -> None:
        path = self._remote(relative_path)
        with self._lock:
            self._assert_safe_target(path)
            self.sandbox.files.write(path, content)

    def dispatch(self, name: str, args: dict, _ctx: dict | None = None) -> str:
        if name not in _ALLOWED_TOOLS:
            return "[blocked] tool is not available in cloud sandbox mode"
        try:
            if name == "bash":
                return self._bash(args)
            if name == "read_file":
                return self._read_file(args)
            if name == "write_file":
                return self._write_file(args)
            if name == "edit_file":
                return self._edit_file(args)
            if name == "list_files":
                return self._list_files(args)
        except Exception as exc:
            return f"[sandbox tool error] {type(exc).__name__}"
        return "[blocked] unknown sandbox tool"

    def _relative_path(self, value: Any = ".") -> str:
        if not isinstance(value, str) or "\x00" in value:
            raise ValueError("invalid workspace path")
        raw = value.strip() or "."
        if raw == _SANDBOX_ROOT:
            return "."
        if raw.startswith(_SANDBOX_ROOT + "/"):
            raw = raw[len(_SANDBOX_ROOT) + 1:]
        elif Path(raw).is_absolute():
            candidate = Path(raw).resolve(strict=False)
            try:
                raw = candidate.relative_to(self.local_workspace).as_posix()
            except ValueError as exc:
                raise ValueError("path is outside the run workspace") from exc
        path = PurePosixPath(raw)
        if path.is_absolute() or any(part in ("..", "") for part in path.parts):
            raise ValueError("path is outside the run workspace")
        return "." if str(path) in ("", ".") else path.as_posix()

    def _remote(self, value: Any = ".") -> str:
        relative = self._relative_path(value)
        return _SANDBOX_ROOT if relative == "." else f"{_SANDBOX_ROOT}/{relative}"

    def _run(self, command: str, *, cwd: str = _SANDBOX_ROOT, timeout: float = 30):
        if self.sandbox is None:
            raise RuntimeError("sandbox is not ready")
        streams = {"stdout": [], "stderr": []}
        lengths = {"stdout": 0, "stderr": 0}

        def capture(which: str):
            def on_chunk(chunk: str) -> None:
                remaining = _MAX_TOOL_OUTPUT - lengths[which]
                if remaining > 0:
                    text = str(chunk)[:remaining]
                    streams[which].append(text)
                    lengths[which] += len(text)
            return on_chunk

        result = self.sandbox.commands.run(
            command,
            cwd=cwd,
            timeout=max(1, min(float(timeout), _MAX_COMMAND_SECONDS)),
            on_stdout=capture("stdout"),
            on_stderr=capture("stderr"),
        )
        stdout = "".join(streams["stdout"])
        stderr = "".join(streams["stderr"])
        # Simple test doubles or future SDK variants may return buffered output
        # without invoking callbacks. Keep the same hard cap for that path.
        if not stdout:
            stdout = str(getattr(result, "stdout", "") or "")[:_MAX_TOOL_OUTPUT]
        if not stderr:
            stderr = str(getattr(result, "stderr", "") or "")[:_MAX_TOOL_OUTPUT]
        exit_code = getattr(result, "exit_code", 0)
        return stdout, stderr, exit_code

    def _bash(self, args: dict) -> str:
        command = args.get("command")
        if not isinstance(command, str) or not command.strip() or len(command) > _MAX_COMMAND_LENGTH:
            return "[error] command must be non-empty and at most 10000 characters"
        cwd = self._remote(args.get("cwd", "."))
        try:
            requested = float(args.get("timeout", 120))
            if not math.isfinite(requested):
                requested = 120
        except (TypeError, ValueError):
            requested = 120
        with self._lock:
            stdout, stderr, code = self._run(command, cwd=cwd, timeout=requested)
        text = (stdout + ("\n" + stderr if stderr else "")).strip()
        return (text or f"(exit code {code})")[:_MAX_TOOL_OUTPUT]

    def _read_file(self, args: dict) -> str:
        relative = self._relative_path(args.get("path"))
        if relative == ".":
            return "[error] path must name a workspace file"
        try:
            limit = int(args.get("limit", 400))
        except (TypeError, ValueError):
            limit = 400
        limit = max(1, min(limit, 400))
        # Walk every path component using O_NOFOLLOW so a shell-created symlink
        # cannot redirect this bridge outside the project tree.
        script = (
            "import os,stat,sys\n"
            f"root={_SANDBOX_ROOT!r}\n"
            "parts=sys.argv[1].split('/')\n"
            "directory_flags=os.O_RDONLY|getattr(os,'O_DIRECTORY',0)|getattr(os,'O_NOFOLLOW',0)\n"
            "fd=os.open(root,directory_flags)\n"
            "for part in parts[:-1]:\n"
            "    next_fd=os.open(part,directory_flags,dir_fd=fd)\n"
            "    os.close(fd)\n"
            "    fd=next_fd\n"
            "fd=os.open(parts[-1],os.O_RDONLY|getattr(os,'O_NOFOLLOW',0),dir_fd=fd)\n"
            "if not stat.S_ISREG(os.fstat(fd).st_mode): raise SystemExit(73)\n"
            f"data=os.read(fd,{_MAX_TOOL_OUTPUT + 1})\n"
            "sys.stdout.write(data.decode('utf-8','replace'))\n"
        )
        command = "python3 -c " + shlex.quote(script) + " " + shlex.quote(relative)
        with self._lock:
            stdout, stderr, code = self._run(command, timeout=30)
        if code:
            return f"[error] {stderr[:500] or 'file could not be read'}"
        lines = stdout.splitlines()
        numbered = "\n".join(f"{i + 1}: {line}" for i, line in enumerate(lines[:limit]))
        suffix = "\n[truncated]" if len(lines) > limit or len(stdout) >= _MAX_TOOL_OUTPUT else ""
        return (numbered + suffix)[:_MAX_TOOL_OUTPUT]

    def _write_file(self, args: dict) -> str:
        path = self._remote(args.get("path"))
        content = args.get("content")
        if not isinstance(content, str):
            return "[error] content must be text"
        if len(content.encode("utf-8")) > _MAX_FILE_BYTES:
            return "[error] file exceeds the 1 MB limit"
        with self._lock:
            self._assert_safe_target(path)
            self.sandbox.files.write(path, content)
        return f"Wrote {path.removeprefix(_SANDBOX_ROOT + '/')}"

    def _edit_file(self, args: dict) -> str:
        path = self._remote(args.get("path"))
        old = args.get("old_text")
        new = args.get("new_text")
        if not isinstance(old, str) or not old or not isinstance(new, str):
            return "[error] old_text and new_text must be non-empty/text values"
        if len(new.encode("utf-8")) > _MAX_FILE_BYTES:
            return "[error] replacement exceeds the 1 MB limit"
        with self._lock:
            self._assert_safe_target(path, allow_missing=False)
            stdout, stderr, code = self._run(
                f"head -c {_MAX_FILE_BYTES} -- {shlex.quote(path)}", timeout=30
            )
            if code:
                return f"[error] {stderr[:500] or 'file could not be read'}"
            if stdout.count(old) != 1:
                return "[error] old_text must match exactly one location"
            updated = stdout.replace(old, new, 1)
            if len(updated.encode("utf-8")) > _MAX_FILE_BYTES:
                return "[error] edited file exceeds the 1 MB limit"
            self._assert_safe_target(path, allow_missing=False)
            self.sandbox.files.write(path, updated)
        return f"Updated {path.removeprefix(_SANDBOX_ROOT + '/')}"

    def _list_files(self, args: dict) -> str:
        remote = self._remote(args.get("path", "."))
        try:
            depth = int(args.get("depth", 2))
        except (TypeError, ValueError):
            depth = 2
        depth = max(1, min(depth, 5))
        command = (f"find {shlex.quote(remote)} -maxdepth {depth} -type f -print "
                   f"2>/dev/null | head -n 200")
        with self._lock:
            stdout, stderr, code = self._run(command, timeout=30)
        if code:
            return f"[error] {stderr[:500] or 'directory could not be listed'}"
        return stdout.replace(_SANDBOX_ROOT + "/", "")[:_MAX_TOOL_OUTPUT] or "(no files)"

    def _assert_safe_target(self, remote_path: str, *, allow_missing: bool = True) -> None:
        # Prevent file API operations from following a symlink planted by a prior
        # sandbox command. The workspace is one-run-only and all remote commands
        # execute sequentially through this bridge.
        script = (
            "python3 -c "
            + shlex.quote(
                "import os,sys; root=os.path.realpath(" + repr(_SANDBOX_ROOT) + "); "
                "target=sys.argv[1]; parent=os.path.realpath(os.path.dirname(target)); "
                "exists=os.path.lexists(target); require_exists=" + ("False" if allow_missing else "True") + "; "
                "ok=(parent==root or parent.startswith(root+os.sep)) and "
                "(not require_exists or exists) and "
                "(not exists or (not os.path.islink(target) and "
                "(os.path.realpath(target)==root or os.path.realpath(target).startswith(root+os.sep)))); "
                "sys.exit(0 if ok else 73)"
            )
            + " " + shlex.quote(remote_path)
        )
        _, _, code = self._run(script, timeout=10)
        if code != 0:
            raise ValueError("workspace path is unsafe")
