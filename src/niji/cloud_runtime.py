"""Provider-neutral run contract and a deterministic local fake adapter.

The local adapter is for development/tests only. Per-run directories are separate
workspaces, not OS/container security boundaries; production cloud workers must add
process/container isolation, durable storage, and provider-specific authentication.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import os
import re
import shutil
import stat
import zipfile
import threading
import time
import uuid
import zlib
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Protocol, runtime_checkable


_MAX_PAYLOAD_BYTES = 1_000_000
_MAX_RESULT_BYTES = 250_000
_MAX_TIMEOUT_SECONDS = 86_400
_MAX_CLOUD_PROMPT_BYTES = 100_000
_MAX_CLOUD_PROJECT_FILES = 100
_MAX_CLOUD_PROJECT_FILE_BYTES = 64_000
_MAX_CLOUD_PROJECT_TOTAL_BYTES = 500_000
_MAX_CLOUD_ARCHIVE_BYTES = 500_000
_MAX_CLOUD_ARCHIVE_ENTRIES = 256
_BLOCKED_CLOUD_PROJECT_NAMES = {".git", ".niji", ".venv", "venv", "node_modules", "__pycache__"}
_BLOCKED_CLOUD_SECRET_NAMES = {"id_rsa", "id_ed25519", "credentials.json", "secrets.json"}
_MAX_ERROR_LENGTH = 1_000
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")


class RunStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    FAILED = "failed"


_TERMINAL = {RunStatus.COMPLETED, RunStatus.CANCELLED, RunStatus.TIMED_OUT, RunStatus.FAILED}


class RunError(RuntimeError):
    """Base exception for invalid run requests and runner operations."""


class IdempotencyConflict(RunError):
    """An idempotency key was reused for a different request."""


class ActiveRunsPreventDeletion(RunError):
    """Tenant run data cannot be deleted while a run is active."""


class RunLimitExceeded(RunError):
    """A tenant exceeded an API-side run or usage limit."""

    def __init__(self, message: str, *, retry_after: int = 60):
        super().__init__(message)
        self.retry_after = max(1, int(retry_after))


class RunCancelled(RunError):
    """Raised when a cooperative worker observes cancellation."""


class RunTimedOut(RunError):
    """Raised when a cooperative worker observes its deadline."""


def _extract_cloud_archive(encoded: str) -> list[dict[str, str]]:
    """Decode a bounded ZIP into text files without writing to the host filesystem."""
    if not isinstance(encoded, str) or len(encoded) > 666_668:
        raise ValueError("project archive exceeds the 500 KB compressed limit")
    try:
        archive_bytes = base64.b64decode(encoded, validate=True)
    except (ValueError, UnicodeEncodeError) as exc:
        raise ValueError("project archive must be valid base64") from exc
    if not archive_bytes or len(archive_bytes) > _MAX_CLOUD_ARCHIVE_BYTES:
        raise ValueError("project archive exceeds the 500 KB compressed limit")

    files: list[dict[str, str]] = []
    total_bytes = 0
    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            entries = archive.infolist()
            if len(entries) > _MAX_CLOUD_ARCHIVE_ENTRIES:
                raise ValueError("project archive contains too many entries")
            for info in entries:
                name = info.filename
                path_name = name[:-1] if info.is_dir() and name.endswith("/") else name
                if (not path_name or len(path_name) > 240 or "\\x00" in path_name
                        or "\\\\" in path_name or path_name.startswith("/")
                        or re.match(r"^[A-Za-z]:", path_name)):
                    raise ValueError("project archive contains an unsafe path")
                parts = path_name.split("/")
                if any(part in ("", ".", "..") for part in parts):
                    raise ValueError("project archive contains an unsafe path")
                mode = info.external_attr >> 16
                if stat.S_ISLNK(mode):
                    raise ValueError("project archives cannot contain symlinks")
                if info.flag_bits & 0x1:
                    raise ValueError("encrypted project archives are not supported")
                if info.is_dir():
                    continue
                if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                    raise ValueError("project archive uses an unsupported compression method")
                if (info.file_size < 0 or info.file_size > _MAX_CLOUD_PROJECT_FILE_BYTES
                        or info.file_size > _MAX_CLOUD_PROJECT_TOTAL_BYTES):
                    raise ValueError("a project archive file exceeds the 64 KB limit")
                total_bytes += info.file_size
                if total_bytes > _MAX_CLOUD_PROJECT_TOTAL_BYTES:
                    raise ValueError("project archive exceeds the 500 KB extracted limit")
                if len(files) >= _MAX_CLOUD_PROJECT_FILES:
                    raise ValueError("project archive contains more than 100 files")
                with archive.open(info, "r") as stream:
                    content_bytes = stream.read(_MAX_CLOUD_PROJECT_FILE_BYTES + 1)
                    if len(content_bytes) > _MAX_CLOUD_PROJECT_FILE_BYTES:
                        raise ValueError("a project archive file exceeds the 64 KB limit")
                    if len(content_bytes) != info.file_size or stream.read(1):
                        raise ValueError("project archive entry size is inconsistent")
                if b"\\x00" in content_bytes:
                    raise ValueError("binary project files are not supported")
                try:
                    content = content_bytes.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise ValueError("project archive files must be UTF-8 text") from exc
                files.append({"path": path_name, "content": content})
    except (zipfile.BadZipFile, EOFError, OSError, RuntimeError, NotImplementedError, zlib.error) as exc:
        raise ValueError("project archive is invalid or unsupported") from exc
    if not files:
        raise ValueError("project archive must contain at least one text file")
    return files


def validate_cloud_payload(payload: Any) -> tuple[str, list[dict[str, str]]]:
    """Validate a bounded prompt and at most one project source.

    Text files and ZIP archives are converted into bounded plain UTF-8 files;
    repository references are validated here and downloaded later by the trusted
    worker. No user-provided archive path is extracted on the worker filesystem.
    """
    source_shapes = (
        {"prompt"}, {"prompt", "files"}, {"prompt", "archive_base64"},
        {"prompt", "repository"},
    )
    if not isinstance(payload, Mapping):
        raise ValueError("Cloud runs require a prompt and at most one bounded project source")
    keys = set(payload)
    if not any(keys == shape or keys == shape | {"model"} for shape in source_shapes):
        raise ValueError("Cloud runs require a prompt and at most one bounded project source")
    if "model" in payload:
        from .cloud_models import is_known_cloud_model
        if not is_known_cloud_model(payload["model"]):
            raise ValueError("Selected model is not available")
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("Cloud runs require one non-empty prompt")
    if len(prompt.encode("utf-8")) > _MAX_CLOUD_PROMPT_BYTES:
        raise ValueError("prompt exceeds the 100 KB limit")
    if "archive_base64" in payload:
        raw_files = _extract_cloud_archive(payload["archive_base64"])
    elif "repository" in payload:
        from .cloud_repository import validate_repository_spec
        validate_repository_spec(payload["repository"])
        raw_files = []
    else:
        raw_files = payload.get("files", [])
    if not isinstance(raw_files, list) or len(raw_files) > _MAX_CLOUD_PROJECT_FILES:
        raise ValueError("project files must be a list of at most 100 files")
    files: list[dict[str, str]] = []
    seen: set[str] = set()
    total_bytes = 0
    for item in raw_files:
        if not isinstance(item, Mapping) or set(item) != {"path", "content"}:
            raise ValueError("each project file requires only path and content")
        path, content = item.get("path"), item.get("content")
        if not isinstance(path, str) or not isinstance(content, str):
            raise ValueError("project paths and contents must be text")
        if (not path or len(path) > 240 or chr(0) in path or chr(92) in path or "\\x00" in path or "\\\\" in path
                or path.startswith("/") or re.match(r"^[A-Za-z]:", path)):
            raise ValueError("project path is invalid")
        pure = PurePosixPath(path)
        if not pure.parts or any(part in ("", ".", "..") for part in pure.parts) or pure.is_absolute():
            raise ValueError("project path must stay inside the workspace")
        normalized = pure.as_posix()
        folded = normalized.casefold()
        if folded in seen:
            raise ValueError("project file paths must be unique")
        seen.add(folded)
        names = [part.casefold() for part in pure.parts]
        if any(name in _BLOCKED_CLOUD_PROJECT_NAMES for name in names):
            raise ValueError("generated and credential folders cannot be uploaded")
        basename = names[-1]
        if (basename in _BLOCKED_CLOUD_SECRET_NAMES
                or (basename.startswith(".env") and basename != ".env.example")
                or basename.endswith((".pem", ".key", ".p12", ".pfx"))):
            raise ValueError("likely secret files cannot be uploaded")
        size = len(content.encode("utf-8"))
        if size > _MAX_CLOUD_PROJECT_FILE_BYTES:
            raise ValueError("a project file exceeds the 64 KB limit")
        total_bytes += size
        if total_bytes > _MAX_CLOUD_PROJECT_TOTAL_BYTES:
            raise ValueError("project files exceed the 500 KB total limit")
        files.append({"path": normalized, "content": content})
    return prompt, files


@dataclass(frozen=True, init=False)
class RunRequest:
    """Immutable JSON request accepted by any conforming runner adapter."""

    idempotency_key: str
    timeout_seconds: float
    _payload_json: str = field(repr=False)

    def __init__(self, idempotency_key: str, payload: Mapping[str, Any], timeout_seconds: float = 900):
        if not isinstance(idempotency_key, str) or not _IDEMPOTENCY_KEY.fullmatch(idempotency_key):
            raise ValueError("idempotency_key must be 1-200 safe characters")
        if not isinstance(payload, Mapping):
            raise ValueError("payload must be a JSON object")
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
            raise ValueError("timeout_seconds must be a finite number")
        timeout = float(timeout_seconds)
        if not math.isfinite(timeout) or timeout <= 0 or timeout > _MAX_TIMEOUT_SECONDS:
            raise ValueError(f"timeout_seconds must be between 0 and {_MAX_TIMEOUT_SECONDS}")
        try:
            payload_json = json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":"), allow_nan=False, sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise ValueError("payload must contain only JSON-safe values") from exc
        if len(payload_json.encode("utf-8")) > _MAX_PAYLOAD_BYTES:
            raise ValueError("payload exceeds the safe 1 MB limit")
        object.__setattr__(self, "idempotency_key", idempotency_key)
        object.__setattr__(self, "timeout_seconds", timeout)
        object.__setattr__(self, "_payload_json", payload_json)

    @property
    def payload(self) -> dict[str, Any]:
        """Return a fresh payload copy so callers cannot mutate the request."""
        return json.loads(self._payload_json)

    @property
    def fingerprint(self) -> str:
        material = f"{self.timeout_seconds}:{self._payload_json}".encode("utf-8")
        return hashlib.sha256(material).hexdigest()


@dataclass(frozen=True)
class RunEvent:
    status: RunStatus
    timestamp: float
    detail: str


@dataclass(frozen=True)
class RunSnapshot:
    run_id: str
    status: RunStatus
    created_at: float
    updated_at: float
    result: Any = None
    error: str | None = None
    events: tuple[RunEvent, ...] = ()


@runtime_checkable
class Runner(Protocol):
    """Minimum provider-neutral interface for submitting and controlling runs."""

    def submit(self, request: RunRequest) -> RunSnapshot: ...
    def get(self, run_id: str) -> RunSnapshot | None: ...
    def cancel(self, run_id: str) -> RunSnapshot: ...


class RunExecutionContext:
    """Cooperative cancellation/deadline checks exposed to a worker callback."""

    def __init__(self, workspace: Path, cancel_event: threading.Event, deadline: float):
        self.workspace = workspace
        self._cancel_event = cancel_event
        self._deadline = deadline

    @property
    def cancellation_requested(self) -> bool:
        return self._cancel_event.is_set()

    def check_cancelled(self) -> None:
        if time.monotonic() >= self._deadline:
            raise RunTimedOut("Run deadline reached")
        if self._cancel_event.is_set():
            raise RunCancelled("Run cancellation requested")


@dataclass
class _RunState:
    run_id: str
    request: RunRequest
    fingerprint: str
    status: RunStatus
    created_at: float
    updated_at: float
    workspace: Path
    cancel_event: threading.Event
    deadline: float
    events: list[RunEvent]
    result_json: str | None = None
    error: str | None = None
    future: Future | None = None


class LocalFakeRunner:
    """Async, in-memory fake runner with per-run workspaces for local testing.

    Results and failures are stored in memory only. Cancellation/deadlines are
    cooperative: handlers should call ``context.check_cancelled()`` at safe points.
    """

    def __init__(
        self,
        workspace_root: str | Path,
        handler: Callable[[RunRequest, RunExecutionContext], Any],
        *,
        max_workers: int = 4,
    ):
        if not callable(handler):
            raise TypeError("handler must be callable")
        if isinstance(max_workers, bool) or not isinstance(max_workers, int) or not 1 <= max_workers <= 64:
            raise ValueError("max_workers must be between 1 and 64")
        self._root = Path(workspace_root).expanduser()
        if self._root.is_symlink():
            raise OSError("Workspace root must not be a symlink")
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self._root.is_symlink() or not self._root.is_dir():
            raise OSError("Workspace root must be a real directory")
        self._root = self._root.resolve()
        if os.name == "posix":
            self._root.chmod(0o700)
        self._handler = handler
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="niji-fake-run")
        self._lock = threading.RLock()
        self._runs: dict[str, _RunState] = {}
        self._by_idempotency_key: dict[str, str] = {}
        self._closed = False

    def submit(self, request: RunRequest) -> RunSnapshot:
        if not isinstance(request, RunRequest):
            raise TypeError("request must be a RunRequest")
        with self._lock:
            if self._closed:
                raise RunError("Runner is closed")
            prior_id = self._by_idempotency_key.get(request.idempotency_key)
            if prior_id is not None:
                prior = self._runs[prior_id]
                if prior.fingerprint != request.fingerprint:
                    raise IdempotencyConflict("Idempotency key already belongs to a different request")
                return self._snapshot(prior)

            run_id = uuid.uuid4().hex
            workspace = self._create_workspace(run_id)
            now = time.time()
            state = _RunState(
                run_id=run_id,
                request=request,
                fingerprint=request.fingerprint,
                status=RunStatus.QUEUED,
                created_at=now,
                updated_at=now,
                workspace=workspace,
                cancel_event=threading.Event(),
                deadline=time.monotonic() + request.timeout_seconds,
                events=[RunEvent(RunStatus.QUEUED, now, "Run accepted")],
            )
            self._runs[run_id] = state
            self._by_idempotency_key[request.idempotency_key] = run_id
            try:
                state.future = self._executor.submit(self._execute, run_id)
            except Exception:
                self._runs.pop(run_id, None)
                self._by_idempotency_key.pop(request.idempotency_key, None)
                self._remove_workspace(workspace)
                raise
            return self._snapshot(state)

    def get(self, run_id: str) -> RunSnapshot | None:
        with self._lock:
            state = self._runs.get(run_id)
            return self._snapshot(state) if state else None

    def cancel(self, run_id: str) -> RunSnapshot:
        with self._lock:
            state = self._runs.get(run_id)
            if state is None:
                raise KeyError("Unknown run id")
            if state.status in _TERMINAL:
                return self._snapshot(state)
            state.cancel_event.set()
            if state.status == RunStatus.QUEUED and state.future is not None and state.future.cancel():
                self._transition(state, RunStatus.CANCELLED, "Cancelled before execution")
            elif state.status in (RunStatus.QUEUED, RunStatus.RUNNING):
                self._transition(state, RunStatus.CANCELLING, "Cancellation requested")
            return self._snapshot(state)

    def workspace_for(self, run_id: str) -> Path:
        """Return the local fake adapter's workspace; never part of cloud contract."""
        with self._lock:
            state = self._runs.get(run_id)
            if state is None:
                raise KeyError("Unknown run id")
            return state.workspace

    def purge(self, run_id: str) -> bool:
        """Delete a terminal run's workspace explicitly; active runs cannot be purged."""
        with self._lock:
            state = self._runs.get(run_id)
            if state is None:
                return False
            if state.status not in _TERMINAL:
                raise RunError("Cannot purge an active run")
            return self._remove_workspace(state.workspace)

    def close(self, *, wait: bool = True) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for state in self._runs.values():
                if state.status in _TERMINAL:
                    continue
                state.cancel_event.set()
                if state.status == RunStatus.QUEUED and state.future is not None and state.future.cancel():
                    self._transition(state, RunStatus.CANCELLED, "Runner closed before execution")
                elif state.status in (RunStatus.QUEUED, RunStatus.RUNNING):
                    self._transition(state, RunStatus.CANCELLING, "Runner shutdown requested cancellation")
        # Workers acquire the same lock when publishing terminal state; never hold it
        # while waiting for executor shutdown.
        self._executor.shutdown(wait=wait, cancel_futures=True)

    def _create_workspace(self, run_id: str) -> Path:
        path = self._root / run_id
        path.mkdir(mode=0o700)
        if path.is_symlink() or path.resolve().parent != self._root:
            raise OSError("Run workspace escaped its isolation root")
        if os.name == "posix":
            path.chmod(0o700)
        return path

    def _remove_workspace(self, path: Path) -> bool:
        # Only remove a real directory directly under the configured root. Do not
        # follow a replaced symlink or recursively delete an unexpected location.
        try:
            if (path.is_symlink() or not path.is_dir()
                    or path.parent.resolve() != self._root):
                return False
            shutil.rmtree(path)
            return not path.exists()
        except OSError:
            return False

    def _execute(self, run_id: str) -> None:
        with self._lock:
            state = self._runs.get(run_id)
            if state is None or state.status in _TERMINAL:
                return
            if state.cancel_event.is_set():
                self._transition(state, RunStatus.CANCELLED, "Cancelled before execution")
                return
            self._transition(state, RunStatus.RUNNING, "Worker started")
            context = RunExecutionContext(state.workspace, state.cancel_event, state.deadline)
            request = state.request

        try:
            context.check_cancelled()
            result = self._handler(request, context)
            context.check_cancelled()
            result_json = json.dumps(result, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            if len(result_json.encode("utf-8")) > _MAX_RESULT_BYTES:
                raise ValueError("result exceeds the safe 250 KB limit")
        except RunCancelled:
            target, detail, error = RunStatus.CANCELLED, "Run cancelled", None
        except RunTimedOut:
            target, detail, error = RunStatus.TIMED_OUT, "Run timed out", "Run deadline reached"
        except Exception as exc:
            # Do not expose arbitrary exception text: it can contain credentials,
            # prompt contents, or other private data from a provider/handler.
            target = RunStatus.FAILED
            detail = "Run failed"
            error = f"Execution failed ({type(exc).__name__})"[:_MAX_ERROR_LENGTH]
        else:
            with self._lock:
                state = self._runs.get(run_id)
                if state is None:
                    return
                state.result_json = result_json
            target, detail, error = RunStatus.COMPLETED, "Run completed", None

        with self._lock:
            state = self._runs.get(run_id)
            if state is None or state.status in _TERMINAL:
                return
            # Cancellation already requested should win unless the deadline expired.
            if state.status == RunStatus.CANCELLING and target == RunStatus.COMPLETED:
                target, detail = RunStatus.CANCELLED, "Run cancelled"
            self._transition(state, target, detail, error=error)

    def _transition(
        self,
        state: _RunState,
        target: RunStatus,
        detail: str,
        *,
        error: str | None = None,
    ) -> None:
        allowed = {
            RunStatus.QUEUED: {RunStatus.RUNNING, RunStatus.CANCELLING, RunStatus.CANCELLED},
            RunStatus.RUNNING: {RunStatus.CANCELLING, RunStatus.COMPLETED, RunStatus.CANCELLED,
                                RunStatus.TIMED_OUT, RunStatus.FAILED},
            RunStatus.CANCELLING: {RunStatus.CANCELLED, RunStatus.TIMED_OUT, RunStatus.FAILED},
        }
        if target not in allowed.get(state.status, set()):
            raise RunError(f"Invalid lifecycle transition: {state.status.value} -> {target.value}")
        now = time.time()
        state.status = target
        state.updated_at = now
        state.error = error
        if target != RunStatus.COMPLETED:
            state.result_json = None
        state.events.append(RunEvent(target, now, detail[:200]))

    @staticmethod
    def _snapshot(state: _RunState) -> RunSnapshot:
        result = json.loads(state.result_json) if state.result_json is not None else None
        return RunSnapshot(
            run_id=state.run_id,
            status=state.status,
            created_at=state.created_at,
            updated_at=state.updated_at,
            result=result,
            error=state.error,
            events=tuple(state.events),
        )
