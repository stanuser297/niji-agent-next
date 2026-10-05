"""PostgreSQL-backed worker for isolated Niji cloud runs.

The worker executes model calls in the trusted service and can lease one tenant's
runs or claim across tenant partitions. Untrusted coding operations require the
separately configured sandbox-backed executor.
"""
from __future__ import annotations

import json
import logging
import os
import re
import signal
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from .cloud_runtime import (RunCancelled, RunError, RunRequest, RunStatus, RunTimedOut,
                            _MAX_RESULT_BYTES)
from .cloud_usage import (DEFAULT_RUN_COMPLETION_TOKENS, DEFAULT_RUN_PROMPT_TOKENS,
                          cost_micros, pricing_from_environment)
from .config import PRESETS
from .postgres_run_store import PostgresRunStore

_LOG = logging.getLogger("niji.cloud_worker")
_TENANT = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")


def _agent_usage(agent: Any, settings: "WorkerSettings") -> dict[str, Any]:
    usage = getattr(agent, "usage", {})
    snapshot = {
        "prompt_tokens": usage.get("prompt_tokens", 0) if isinstance(usage, dict) else 0,
        "completion_tokens": usage.get("completion_tokens", 0) if isinstance(usage, dict) else 0,
        "usage_reported": bool(getattr(agent, "usage_reported", False)),
        "usage_complete": bool(getattr(agent, "usage_complete", False)),
    }
    snapshot["cost_micros"] = cost_micros(
        snapshot["prompt_tokens"], snapshot["completion_tokens"],
        settings.input_price_micros_per_million,
        settings.output_price_micros_per_million,
    )
    return snapshot


def _bounded_result(result: Any) -> Any:
    """Keep terminal results within the durable store contract."""
    def encode(value: Any) -> bytes:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                          allow_nan=False).encode("utf-8")

    try:
        if len(encode(result)) <= _MAX_RESULT_BYTES:
            return result
    except (TypeError, ValueError):
        raise ValueError("Execution result is not JSON-safe") from None

    if isinstance(result, dict) and set(result) == {"text"} and isinstance(result["text"], str):
        original = result["text"]
        suffix = " … [response truncated to fit the hosted result limit]"
        low, high = 0, len(original)
        while low < high:
            mid = (low + high + 1) // 2
            candidate = {"text": original[:mid] + suffix}
            if len(encode(candidate)) <= _MAX_RESULT_BYTES:
                low = mid
            else:
                high = mid - 1
        bounded = {"text": original[:low] + suffix}
        if len(encode(bounded)) <= _MAX_RESULT_BYTES:
            return bounded
    raise ValueError("Execution result exceeds the hosted result limit")


@dataclass(frozen=True)
class WorkerSettings:
    database_url: str
    tenant_id: str | None
    provider: str
    base_url: str
    model: str
    provider_api_key: str
    poll_seconds: float = 2.0
    lease_seconds: float = 90.0
    max_attempts: int = 3
    max_turns: int = 8
    execution_mode: str = "prompt"
    artifact_retention_days: int = 7
    run_prompt_token_cap: int = DEFAULT_RUN_PROMPT_TOKENS
    run_completion_token_cap: int = DEFAULT_RUN_COMPLETION_TOKENS
    input_price_micros_per_million: int = 0
    output_price_micros_per_million: int = 0

    @classmethod
    def from_env(cls) -> "WorkerSettings":
        database_url = os.environ.get("NIJI_CLOUD_DATABASE_URL", "").strip()
        tenant_mode = os.environ.get("NIJI_CLOUD_WORKER_TENANT_MODE", "single").strip().lower()
        if tenant_mode not in {"single", "all"}:
            raise ValueError("NIJI_CLOUD_WORKER_TENANT_MODE must be 'single' or 'all'")
        tenant_id = os.environ.get("NIJI_CLOUD_TENANT_ID", "").strip()
        if not database_url:
            raise ValueError("NIJI_CLOUD_DATABASE_URL is required for the hosted worker")
        if tenant_mode == "all":
            tenant_id = None
        elif not _TENANT.fullmatch(tenant_id):
            raise ValueError("NIJI_CLOUD_TENANT_ID must be 1-200 safe characters")
        provider = os.environ.get("NIJI_CLOUD_PROVIDER", "openai").strip().lower()
        preset = PRESETS.get(provider)
        base_url = os.environ.get("NIJI_CLOUD_BASE_URL", "").strip() or (
            preset["base_url"] if preset else ""
        )
        model = os.environ.get("NIJI_CLOUD_MODEL", "").strip() or (
            preset["model"] if preset else ""
        )
        api_key = os.environ.get("NIJI_CLOUD_PROVIDER_API_KEY", "")
        execution_mode = os.environ.get("NIJI_CLOUD_EXECUTION_MODE", "prompt").strip().lower()
        if execution_mode not in {"prompt", "sandbox"}:
            raise ValueError("NIJI_CLOUD_EXECUTION_MODE must be 'prompt' or 'sandbox'")
        if execution_mode == "sandbox" and not os.environ.get("E2B_API_KEY", ""):
            raise ValueError("E2B_API_KEY is required for sandbox execution mode")
        if not base_url or not model:
            raise ValueError("Configure a known NIJI_CLOUD_PROVIDER or set NIJI_CLOUD_BASE_URL and NIJI_CLOUD_MODEL")
        if not api_key:
            raise ValueError("NIJI_CLOUD_PROVIDER_API_KEY is required; set it as a private worker secret")
        def number(name: str, default: float, low: float, high: float) -> float:
            try:
                value = float(os.environ.get(name, str(default)))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{name} must be a number") from exc
            if not low <= value <= high:
                raise ValueError(f"{name} must be between {low} and {high}")
            return value
        def token_limit(name: str, default: int) -> int:
            raw = os.environ.get(name, str(default)).strip()
            try:
                value = int(raw)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{name} must be a positive integer") from exc
            if str(value) != raw or not 1 <= value <= 10_000_000_000:
                raise ValueError(f"{name} must be between 1 and 10000000000")
            return value
        max_attempts = int(number("NIJI_CLOUD_MAX_ATTEMPTS", 3, 1, 10))
        max_turns = int(number("NIJI_CLOUD_MAX_TURNS", 8, 1, 30))
        retention_value = number("NIJI_CLOUD_ARTIFACT_RETENTION_DAYS", 7, 1, 90)
        if not retention_value.is_integer():
            raise ValueError("NIJI_CLOUD_ARTIFACT_RETENTION_DAYS must be a whole number")
        prompt_cap = token_limit("NIJI_CLOUD_RUN_PROMPT_TOKEN_CAP", DEFAULT_RUN_PROMPT_TOKENS)
        completion_cap = token_limit(
            "NIJI_CLOUD_RUN_COMPLETION_TOKEN_CAP", DEFAULT_RUN_COMPLETION_TOKENS)
        _monthly_cost, input_price, output_price = pricing_from_environment()
        return cls(
            database_url, tenant_id, provider, base_url, model, api_key,
            number("NIJI_CLOUD_POLL_SECONDS", 2.0, 0.2, 60),
            number("NIJI_CLOUD_LEASE_SECONDS", 90.0, 15, 3600),
            max_attempts, max_turns, execution_mode, int(retention_value),
            prompt_cap, completion_cap, input_price, output_price,
        )


class CloudRunContext:
    """Cancellation/deadline state shared by the executor and lease heartbeat."""

    def __init__(self, deadline: float):
        self.deadline = deadline
        self._cancelled = threading.Event()
        self._timed_out = threading.Event()
        self._lock = threading.Lock()
        self._cancel_callback: Callable[[], Any] | None = None

    @property
    def cancellation_requested(self) -> bool:
        return self._cancelled.is_set()

    def set_cancel_callback(self, callback: Callable[[], Any]) -> None:
        call_now = False
        with self._lock:
            self._cancel_callback = callback
            call_now = self._cancelled.is_set()
        if call_now:
            callback()

    def cancel(self) -> None:
        self._cancelled.set()
        with self._lock:
            callback = self._cancel_callback
        if callback is not None:
            try:
                callback()
            except Exception:
                _LOG.debug("Cancellation callback failed", exc_info=True)

    def timeout(self) -> None:
        self._timed_out.set()
        self.cancel()

    def check_cancelled(self) -> None:
        if time.monotonic() >= self.deadline:
            self.timeout()
        if self._timed_out.is_set():
            raise RunTimedOut("Run deadline reached")
        if self._cancelled.is_set():
            raise RunCancelled("Run cancellation requested")


class NijiPromptExecutor:
    """Run a Niji model session with all host-side tools disabled.

    This is the safe first execution mode. Tool-enabled coding tasks are withheld
    until the sandbox adapter is implemented and independently verified.
    """

    def __init__(self, settings: WorkerSettings):
        self.settings = settings
        self._activity_callback = None
        self.usage: dict[str, Any] | None = None

    def set_activity_callback(self, callback: Callable[[dict[str, Any]], Any]) -> None:
        self._activity_callback = callback

    def __call__(self, request: RunRequest, context: CloudRunContext) -> dict[str, str]:
        payload = request.payload
        prompt = payload.get("prompt")
        if (set(payload) not in ({"prompt"}, {"prompt", "model"})
                or not isinstance(prompt, str) or not prompt.strip()):
            raise ValueError("Cloud prompt runs require a payload with one non-empty 'prompt' field")
        if len(prompt.encode("utf-8")) > 100_000:
            raise ValueError("prompt exceeds the 100 KB limit")
        selected_model = self.settings.model
        if "model" in payload:
            from .cloud_models import resolve_cloud_model
            selected_model = resolve_cloud_model(payload["model"], self.settings.provider)
            if selected_model is None:
                raise ValueError("Selected model is not available")
        context.check_cancelled()
        from .agent import Agent
        provider_cfg = {
            "provider": self.settings.provider,
            "base_url": self.settings.base_url,
            "model": selected_model,
            "api_key": self.settings.provider_api_key,
        }
        with tempfile.TemporaryDirectory(prefix="niji-cloud-") as workspace:
            agent = Agent(
                provider_cfg,
                approval="ask",
                max_turns=self.settings.max_turns,
                max_tool_calls=1,
                max_tool_calls_per_turn=1,
                verbose=False,
                mcp_clients=[],
                allowed_tools=[],
                workspace=workspace,
                cloud_mode=True,
                cloud_prompt_token_limit=self.settings.run_prompt_token_cap,
                cloud_completion_token_limit=self.settings.run_completion_token_cap,
            )
            context.set_cancel_callback(agent.cancel)
            agent.activity_callback = self._activity_callback
            try:
                result = agent.chat(prompt)
            finally:
                self.usage = _agent_usage(agent, self.settings)
            context.check_cancelled()
            if not isinstance(result, str):
                raise ValueError("Agent returned a non-text response")
            return {"text": result}


class CloudWorker:
    """Claim, execute, renew, and safely finalize one PostgreSQL run at a time."""

    def __init__(self, store: Any, tenant_id: str, executor: Callable[[RunRequest, CloudRunContext], Any],
                 *, lease_seconds: float = 90, max_attempts: int = 3,
                 poll_seconds: float = 2, artifact_retention_days: int = 7):
        if (isinstance(artifact_retention_days, bool) or not isinstance(artifact_retention_days, int)
                or not 1 <= artifact_retention_days <= 90):
            raise ValueError("artifact_retention_days must be between 1 and 90")
        self.store = store
        self.tenant_id = tenant_id
        self.executor = executor
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts
        self.poll_seconds = poll_seconds
        self.artifact_retention_days = artifact_retention_days
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run_once(self) -> bool:
        claim = self.store.claim_next(
            self.tenant_id, lease_seconds=self.lease_seconds, max_attempts=self.max_attempts
        )
        if claim is None:
            return False
        self._execute_claim(claim)
        return True

    def run_forever(self) -> None:
        next_artifact_cleanup = 0.0
        while not self._stop.is_set():
            try:
                now = time.monotonic()
                if now >= next_artifact_cleanup:
                    next_artifact_cleanup = now + 3600
                    cleanup = getattr(self.store, "cleanup_expired_artifacts", None)
                    if callable(cleanup):
                        try:
                            removed = cleanup(retention_days=self.artifact_retention_days)
                        except Exception as exc:
                            _LOG.warning("Artifact retention cleanup failed (%s)", type(exc).__name__)
                        else:
                            if removed:
                                _LOG.info("Expired cloud artifacts removed: %s", removed)
                if not self.run_once():
                    self._stop.wait(self.poll_seconds)
            except Exception as exc:
                _LOG.warning("Worker poll failed (%s)", type(exc).__name__)
                self._stop.wait(min(self.poll_seconds * 2, 30))

    def _execute_claim(self, claim) -> None:
        tenant_id = getattr(claim, "tenant_id", None) or self.tenant_id
        if tenant_id is None:
            raise RunError("A claimed run has no tenant owner")
        context = CloudRunContext(time.monotonic() + claim.request.timeout_seconds)
        if hasattr(self.executor, "usage"):
            self.executor.usage = None
        heartbeat_stop = threading.Event()
        lease_lost = threading.Event()
        progress_state = {"last": 0.0, "phase": ""}
        progress_lock = threading.Lock()

        def record_progress(phase: str) -> None:
            now = time.monotonic()
            with progress_lock:
                if phase == progress_state["phase"]:
                    return
                progress_state.update(last=now, phase=phase)
            record = getattr(self.store, "record_progress", None)
            if callable(record):
                try:
                    record(tenant_id, claim.run_id, claim.lease_token, phase)
                except Exception:
                    _LOG.debug("Could not record run progress", exc_info=True)

        def heartbeat() -> None:
            interval = max(1.0, min(self.lease_seconds / 3, 20.0))
            while not heartbeat_stop.wait(interval):
                try:
                    snapshot = self.store.get(tenant_id, claim.run_id)
                    if snapshot is None or snapshot.status in {
                        RunStatus.CANCELLED, RunStatus.TIMED_OUT,
                        RunStatus.FAILED, RunStatus.COMPLETED,
                    }:
                        context.cancel()
                        return
                    if snapshot.status == RunStatus.CANCELLING:
                        # Keep the live lease while a provider request responds to
                        # cancellation; otherwise another worker can duplicate it.
                        context.cancel()
                    elif (time.monotonic() >= context.deadline
                            and not context.cancellation_requested):
                        # Deadline expiry requests cancellation but does not relinquish
                        # ownership until the in-flight provider call has actually exited.
                        context.timeout()
                    if not self.store.renew_lease(
                        tenant_id, claim.run_id, claim.lease_token,
                        lease_seconds=self.lease_seconds,
                    ):
                        lease_lost.set()
                        context.cancel()
                        return
                except Exception as exc:
                    # A transient database error means ownership is uncertain. Stop
                    # execution rather than risk overlapping work after the lease expires.
                    # Keep diagnostics out of logs; they may contain a DSN or secret.
                    _LOG.warning("Could not renew active run lease (%s)", type(exc).__name__)
                    lease_lost.set()
                    context.cancel()
                    return

        heartbeat_thread = threading.Thread(target=heartbeat, name="niji-lease-heartbeat", daemon=True)
        heartbeat_thread.start()
        try:
            # Agent activity content can contain user text or filesystem details.
            # Publish only fixed, low-information phase labels to the run timeline.
            def activity(event: dict[str, Any]) -> None:
                phase = {
                    "THINKING": "Agent is thinking",
                    "TOOL": "Agent is working",
                    "PLAN": "Agent is planning",
                    "DONE": "Response ready",
                }.get(event.get("level"))
                if phase:
                    record_progress(phase)

            setter = getattr(self.executor, "set_activity_callback", None)
            if callable(setter):
                setter(activity)
            artifact_setter = getattr(self.executor, "set_artifact_callback", None)
            artifact_writer = getattr(self.store, "add_artifact", None)
            if callable(artifact_setter) and callable(artifact_writer):
                def save_artifact(path: str, content: bytes, content_type: str) -> None:
                    context.check_cancelled()
                    if lease_lost.is_set():
                        raise RunCancelled("Worker lease lost")
                    artifact_writer(
                        tenant_id, claim.run_id, claim.lease_token,
                        path, content, content_type,
                    )
                artifact_setter(save_artifact)
            context.check_cancelled()
            result = self.executor(claim.request, context)
            context.check_cancelled()
            result = _bounded_result(result)
            if lease_lost.is_set():
                raise RunCancelled("Worker lease lost")
        except RunCancelled:
            target, detail, result, error = RunStatus.CANCELLED, "Run cancelled", None, None
        except RunTimedOut:
            target, detail, result, error = RunStatus.TIMED_OUT, "Run timed out", None, "Run deadline reached"
        except Exception as exc:
            target, detail, result = RunStatus.FAILED, "Run failed", None
            error = f"Execution failed ({type(exc).__name__})"
            _LOG.warning("Cloud run execution failed (%s)", type(exc).__name__)
        else:
            target, detail, error = RunStatus.COMPLETED, "Run completed", None
        finally:
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=max(1.0, min(self.lease_seconds / 3, 20.0)))

        try:
            self.store.finish_claim(
                tenant_id, claim.run_id, claim.lease_token, target, detail,
                result=result, error=error,
                usage=getattr(self.executor, "usage", None),
            )
        except RunError:
            # The API may have cancelled the run or a newer worker may own it.
            _LOG.info("Run state changed before worker could publish its result")


def create_worker(settings: WorkerSettings | None = None) -> CloudWorker:
    settings = settings or WorkerSettings.from_env()
    store = PostgresRunStore(settings.database_url)
    executor = NijiPromptExecutor(settings)
    if settings.execution_mode == "sandbox":
        from .cloud_sandbox import E2BSandboxExecutor
        executor = E2BSandboxExecutor(settings)
    return CloudWorker(
        store, settings.tenant_id, executor,
        lease_seconds=settings.lease_seconds,
        max_attempts=settings.max_attempts,
        poll_seconds=settings.poll_seconds,
        artifact_retention_days=settings.artifact_retention_days,
    )


def main() -> None:
    logging.basicConfig(level=os.environ.get("NIJI_CLOUD_LOG_LEVEL", "INFO"))
    worker = create_worker()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda _signum, _frame: worker.stop())
    _LOG.info("Niji cloud worker started; execution mode=%s", worker.executor.__class__.__name__)
    worker.run_forever()


if __name__ == "__main__":  # pragma: no cover
    main()
