"""Multi-worker PostgreSQL run store for Render and other hosted deployments.

Unlike SQLiteRunStore, this adapter supports multiple API/worker processes through
PostgreSQL transactions and ``FOR UPDATE SKIP LOCKED`` leases. It is persistence
and queue coordination—not a sandbox or authentication boundary.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import secrets
import time
import uuid
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from .cloud_runtime import (
    ActiveRunsPreventDeletion,
    IdempotencyConflict,
    RunError,
    RunLimitExceeded,
    RunEvent,
    RunRequest,
    RunSnapshot,
    RunStatus,
    _MAX_ERROR_LENGTH,
    _MAX_RESULT_BYTES,
    _TERMINAL,
)
from .cloud_usage import (current_month_start, seconds_until_month_end,
                          validate_cost_micros, validate_token_count)
from .durable_run_store import RunClaim, SQLiteRunStore

_SCHEMA_VERSION = 7
_MAX_LIST_LIMIT = 100
_ALLOWED_TRANSITIONS = {
    RunStatus.QUEUED: {RunStatus.RUNNING, RunStatus.CANCELLING, RunStatus.CANCELLED},
    RunStatus.RUNNING: {
        RunStatus.CANCELLING, RunStatus.COMPLETED, RunStatus.CANCELLED,
        RunStatus.TIMED_OUT, RunStatus.FAILED,
    },
    RunStatus.CANCELLING: {RunStatus.CANCELLED, RunStatus.TIMED_OUT, RunStatus.FAILED},
}
_MAX_ARTIFACTS_PER_RUN = 50
_MAX_ARTIFACT_BYTES = 1_000_000
_MAX_ARTIFACT_TOTAL_BYTES = 5_000_000
_BLOCKED_ARTIFACT_DIRS = {".git", ".niji", ".venv", "venv", "node_modules", "__pycache__"}
_BLOCKED_ARTIFACT_NAMES = {".env", "credentials.json", "secrets.json", "id_rsa", "id_ed25519"}


@dataclass(frozen=True)
class StoredArtifact:
    artifact_id: str
    path: str
    content_type: str
    sha256: str
    size_bytes: int
    created_at: float
    data: bytes | None = None


class PostgresRunStore:
    """PostgreSQL-backed run storage and atomic multi-worker queue.

    The DSN must be supplied by trusted server configuration. Tenant IDs remain a
    server-side authorization partition; callers must never choose them directly.
    """

    def __init__(self, dsn: str):
        if not isinstance(dsn, str) or not dsn.strip():
            raise ValueError("A PostgreSQL database URL is required")
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise RuntimeError(
                "Install Niji's cloud extra with PostgreSQL support: "
                "pip install 'niji-agent[cloud]'"
            ) from exc
        self._psycopg = psycopg
        self._dict_row = dict_row
        self._dsn = dsn
        self._initialize()

    def _connect(self):
        return self._psycopg.connect(self._dsn, row_factory=self._dict_row)

    def healthcheck(self) -> bool:
        """Return whether PostgreSQL can answer a lightweight query."""
        with self._connect() as connection:
            row = connection.execute("SELECT 1 AS healthy").fetchone()
            return row is not None and row["healthy"] == 1

    def _initialize(self) -> None:
        with self._connect() as connection:
            # Serialize bootstrap as well as migrations. This avoids concurrent
            # CREATE INDEX/claim deadlocks when API and worker start together.
            connection.execute("SELECT pg_advisory_xact_lock(%s)", (1_213_024_321,))
            connection.execute(
                """CREATE TABLE IF NOT EXISTS niji_run_schema (
                    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
                    version INTEGER NOT NULL
                )"""
            )
            connection.execute(
                "INSERT INTO niji_run_schema (singleton, version) VALUES (TRUE, 0) "
                "ON CONFLICT (singleton) DO NOTHING"
            )
            version_row = connection.execute(
                "SELECT version FROM niji_run_schema WHERE singleton=TRUE FOR UPDATE"
            ).fetchone()
            version = version_row["version"]
            if version > _SCHEMA_VERSION:
                raise RunError("PostgreSQL run store was created by a newer Niji version")
            if version == _SCHEMA_VERSION:
                return
            if version == 0:
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS niji_runs (
                        run_id TEXT PRIMARY KEY,
                        tenant_id TEXT NOT NULL,
                        idempotency_key TEXT NOT NULL,
                        fingerprint TEXT NOT NULL,
                        request_json TEXT NOT NULL,
                        timeout_seconds DOUBLE PRECISION NOT NULL,
                        status TEXT NOT NULL,
                        created_at DOUBLE PRECISION NOT NULL,
                        updated_at DOUBLE PRECISION NOT NULL,
                        result_json TEXT,
                        error TEXT,
                        lease_token_hash TEXT,
                        lease_expires_at DOUBLE PRECISION,
                        attempt_count INTEGER NOT NULL DEFAULT 0,
                        UNIQUE (tenant_id, idempotency_key)
                    )"""
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS niji_runs_tenant_updated "
                    "ON niji_runs (tenant_id, updated_at DESC)"
                )
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS niji_run_events (
                        event_id BIGSERIAL PRIMARY KEY,
                        run_id TEXT NOT NULL REFERENCES niji_runs(run_id) ON DELETE CASCADE,
                        status TEXT NOT NULL,
                        timestamp DOUBLE PRECISION NOT NULL,
                        detail TEXT NOT NULL
                    )"""
                )
            else:
                self._validate_existing_schema(connection, version)
            if version <= 1:
                # Version 1 already contains the complete run/event schema;
                # reject partial or incompatible databases instead of silently
                # marking them upgraded and failing later at runtime.
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS niji_cloud_artifacts (
                        artifact_id TEXT PRIMARY KEY,
                        tenant_id TEXT NOT NULL,
                        run_id TEXT NOT NULL REFERENCES niji_runs(run_id) ON DELETE CASCADE,
                        path TEXT NOT NULL,
                        content_type TEXT NOT NULL,
                        sha256 TEXT NOT NULL,
                        size_bytes INTEGER NOT NULL,
                        content BYTEA NOT NULL,
                        created_at DOUBLE PRECISION NOT NULL,
                        UNIQUE (tenant_id, run_id, path)
                    )"""
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS niji_cloud_artifacts_run "
                    "ON niji_cloud_artifacts (tenant_id, run_id, created_at, artifact_id)"
                )
            if version <= 2:
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS niji_cloud_artifacts_created "
                    "ON niji_cloud_artifacts (created_at, run_id)"
                )
            if version <= 3:
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS niji_runs_tenant_created "
                    "ON niji_runs (tenant_id, created_at)"
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS niji_runs_tenant_status "
                    "ON niji_runs (tenant_id, status)"
                )
            if version <= 4:
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS niji_runs_status_created "
                    "ON niji_runs (status, created_at, run_id)"
                )
            if version <= 5:
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS niji_rate_limit_buckets (
                        bucket_key TEXT NOT NULL,
                        window_start BIGINT NOT NULL,
                        window_end BIGINT NOT NULL,
                        request_count BIGINT NOT NULL CHECK (request_count >= 0),
                        PRIMARY KEY (bucket_key, window_start)
                    )"""
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS niji_rate_limit_buckets_by_window "
                    "ON niji_rate_limit_buckets (window_end)"
                )
            if version <= 6:
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS niji_monthly_usage (
                        tenant_id TEXT NOT NULL,
                        month_start BIGINT NOT NULL,
                        prompt_tokens_used BIGINT NOT NULL DEFAULT 0 CHECK (prompt_tokens_used >= 0),
                        completion_tokens_used BIGINT NOT NULL DEFAULT 0 CHECK (completion_tokens_used >= 0),
                        prompt_tokens_reserved BIGINT NOT NULL DEFAULT 0 CHECK (prompt_tokens_reserved >= 0),
                        completion_tokens_reserved BIGINT NOT NULL DEFAULT 0 CHECK (completion_tokens_reserved >= 0),
                        cost_micros_used BIGINT NOT NULL DEFAULT 0 CHECK (cost_micros_used >= 0),
                        cost_micros_reserved BIGINT NOT NULL DEFAULT 0 CHECK (cost_micros_reserved >= 0),
                        PRIMARY KEY (tenant_id, month_start)
                    )"""
                )
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS niji_run_usage_reservations (
                        run_id TEXT PRIMARY KEY REFERENCES niji_runs(run_id) ON DELETE CASCADE,
                        tenant_id TEXT NOT NULL,
                        month_start BIGINT NOT NULL,
                        reserved_prompt_tokens BIGINT NOT NULL CHECK (reserved_prompt_tokens >= 0),
                        reserved_completion_tokens BIGINT NOT NULL CHECK (reserved_completion_tokens >= 0),
                        reserved_cost_micros BIGINT NOT NULL DEFAULT 0 CHECK (reserved_cost_micros >= 0),
                        actual_prompt_tokens BIGINT,
                        actual_completion_tokens BIGINT,
                        actual_cost_micros BIGINT,
                        settled BOOLEAN NOT NULL DEFAULT FALSE
                    )"""
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS niji_monthly_usage_by_month ON niji_monthly_usage (month_start)"
                )
            # A guarded update makes schema versions monotonic for this migrator.
            connection.execute(
                "UPDATE niji_run_schema SET version=%s "
                "WHERE singleton=TRUE AND version < %s",
                (_SCHEMA_VERSION, _SCHEMA_VERSION),
            )

    @staticmethod
    def _validate_existing_schema(connection, version: int) -> None:
        """Fail fast when an older version marker hides an incomplete schema."""
        required = {
            "niji_runs": {
                "run_id", "tenant_id", "idempotency_key", "fingerprint", "request_json",
                "timeout_seconds", "status", "created_at", "updated_at", "result_json",
                "error", "lease_token_hash", "lease_expires_at", "attempt_count",
            },
            "niji_run_events": {"event_id", "run_id", "status", "timestamp", "detail"},
        }
        if version >= 2:
            required["niji_cloud_artifacts"] = {
                "artifact_id", "tenant_id", "run_id", "path", "content_type", "sha256",
                "size_bytes", "content", "created_at",
            }
        found: dict[str, set[str]] = {table: set() for table in required}
        rows = connection.execute(
            "SELECT table_name,column_name FROM information_schema.columns "
            "WHERE table_schema=current_schema() AND table_name = ANY(%s)",
            (list(required),),
        ).fetchall()
        for row in rows:
            found[row["table_name"]].add(row["column_name"])
        missing = [f"{table}.{column}" for table, columns in required.items()
                   for column in sorted(columns - found[table])]
        if missing:
            raise RunError("PostgreSQL schema is incomplete: " + ", ".join(missing))

    @staticmethod
    def _validate_tenant(tenant_id: str) -> None:
        SQLiteRunStore._validate_tenant(tenant_id)

    @staticmethod
    def _validate_run_id(run_id: str) -> None:
        SQLiteRunStore._validate_run_id(run_id)

    def consume_rate_limit(
        self, bucket_key: str, *, limit: int, window_seconds: int = 60,
    ) -> int | None:
        """Atomically consume one shared hashed request bucket across API replicas."""
        if not isinstance(bucket_key, str) or not re.fullmatch(r"[a-f0-9]{64}", bucket_key):
            raise ValueError("bucket_key must be a lowercase SHA-256 digest")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100_000:
            raise ValueError("limit must be an integer between 1 and 100000")
        if (isinstance(window_seconds, bool) or not isinstance(window_seconds, int)
                or not 1 <= window_seconds <= 86_400):
            raise ValueError("window_seconds must be between 1 and 86400")
        now = time.time()
        window_start = int(now // window_seconds) * window_seconds
        window_end = window_start + window_seconds
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM niji_rate_limit_buckets WHERE window_end <= %s", (int(now),)
            )
            row = connection.execute(
                "INSERT INTO niji_rate_limit_buckets "
                "(bucket_key,window_start,window_end,request_count) VALUES (%s,%s,%s,1) "
                "ON CONFLICT (bucket_key,window_start) DO UPDATE SET "
                "request_count=LEAST(niji_rate_limit_buckets.request_count+1,%s) "
                "RETURNING request_count",
                (bucket_key, window_start, window_end, limit + 1),
            ).fetchone()
        if row["request_count"] <= limit:
            return None
        return max(1, window_end - int(now))

    @staticmethod
    def _snapshot(connection, tenant_id: str, run_id: str) -> RunSnapshot | None:
        row = connection.execute(
            "SELECT run_id, status, created_at, updated_at, result_json, error "
            "FROM niji_runs WHERE tenant_id=%s AND run_id=%s",
            (tenant_id, run_id),
        ).fetchone()
        if row is None:
            return None
        events = connection.execute(
            "SELECT status, timestamp, detail FROM niji_run_events "
            "WHERE run_id=%s ORDER BY event_id", (run_id,),
        ).fetchall()
        return RunSnapshot(
            run_id=row["run_id"], status=RunStatus(row["status"]),
            created_at=row["created_at"], updated_at=row["updated_at"],
            result=json.loads(row["result_json"]) if row["result_json"] is not None else None,
            error=row["error"],
            events=tuple(RunEvent(RunStatus(event["status"]), event["timestamp"], event["detail"])
                        for event in events),
        )

    def create(self, tenant_id: str, request: RunRequest) -> tuple[RunSnapshot, bool]:
        self._validate_tenant(tenant_id)
        if not isinstance(request, RunRequest):
            raise TypeError("request must be a RunRequest")
        now = time.time()
        run_id = uuid.uuid4().hex
        with self._connect() as connection:
            inserted = connection.execute(
                "INSERT INTO niji_runs (run_id, tenant_id, idempotency_key, fingerprint, "
                "request_json, timeout_seconds, status, created_at, updated_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (tenant_id, idempotency_key) DO NOTHING RETURNING run_id",
                (run_id, tenant_id, request.idempotency_key, request.fingerprint,
                 request._payload_json, request.timeout_seconds, RunStatus.QUEUED.value, now, now),
            ).fetchone()
            if inserted is not None:
                connection.execute(
                    "INSERT INTO niji_run_events (run_id,status,timestamp,detail) VALUES (%s,%s,%s,%s)",
                    (run_id, RunStatus.QUEUED.value, now, "Run accepted"),
                )
                snapshot = self._snapshot(connection, tenant_id, run_id)
                return snapshot, True
            existing = connection.execute(
                "SELECT run_id, fingerprint FROM niji_runs WHERE tenant_id=%s AND idempotency_key=%s",
                (tenant_id, request.idempotency_key),
            ).fetchone()
            if existing is None:
                raise RunError("Idempotent run record could not be loaded")
            if existing["fingerprint"] != request.fingerprint:
                raise IdempotencyConflict("Idempotency key already belongs to a different request")
            snapshot = self._snapshot(connection, tenant_id, existing["run_id"])
            return snapshot, False

    def create_limited(
        self, tenant_id: str, request: RunRequest, *,
        max_active_runs: int = 5, max_submissions_per_hour: int = 20,
        monthly_prompt_token_limit: int | None = None,
        monthly_completion_token_limit: int | None = None,
        reserved_prompt_tokens: int = 0, reserved_completion_tokens: int = 0,
        monthly_cost_limit_micros: int | None = None, reserved_cost_micros: int = 0,
    ) -> tuple[RunSnapshot, bool]:
        """Atomically enforce per-tenant queue and hourly submission limits."""
        self._validate_tenant(tenant_id)
        if not isinstance(request, RunRequest):
            raise TypeError("request must be a RunRequest")
        for name, value, maximum in (
            ("max_active_runs", max_active_runs, 100),
            ("max_submissions_per_hour", max_submissions_per_hour, 1000),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
                raise ValueError(f"{name} must be between 1 and {maximum}")
        for name, value in (("reserved_prompt_tokens", reserved_prompt_tokens),
                            ("reserved_completion_tokens", reserved_completion_tokens)):
            validate_token_count(value, name)
        validate_cost_micros(reserved_cost_micros, "reserved_cost_micros")
        for name, value in (("monthly_prompt_token_limit", monthly_prompt_token_limit),
                            ("monthly_completion_token_limit", monthly_completion_token_limit)):
            if value is not None:
                validate_token_count(value, name)
        if (monthly_prompt_token_limit is None) != (monthly_completion_token_limit is None):
            raise ValueError("both monthly token limits must be configured together")
        if monthly_cost_limit_micros is not None:
            validate_cost_micros(monthly_cost_limit_micros, "monthly_cost_limit_micros")
            if monthly_cost_limit_micros <= 0:
                raise ValueError("monthly_cost_limit_micros must be positive")
        if monthly_cost_limit_micros is None and reserved_cost_micros:
            raise ValueError("reserved cost requires a monthly cost limit")
        now = time.time()
        hour_start = int(now // 3600) * 3600
        run_id = uuid.uuid4().hex
        with self._connect() as connection:
            # All API submissions for this tenant serialize around quota checks;
            # the DB indexes bound active/hourly counts as history accumulates.
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (tenant_id,)
            )
            existing = connection.execute(
                "SELECT run_id,fingerprint FROM niji_runs WHERE tenant_id=%s AND idempotency_key=%s",
                (tenant_id, request.idempotency_key),
            ).fetchone()
            if existing is not None:
                if existing["fingerprint"] != request.fingerprint:
                    raise IdempotencyConflict(
                        "Idempotency key already belongs to a different request"
                    )
                snapshot = self._snapshot(connection, tenant_id, existing["run_id"])
                return snapshot, False
            active = connection.execute(
                "SELECT COUNT(*) AS n FROM niji_runs WHERE tenant_id=%s AND status IN (%s,%s,%s)",
                (tenant_id, RunStatus.QUEUED.value, RunStatus.RUNNING.value,
                 RunStatus.CANCELLING.value),
            ).fetchone()["n"]
            if active >= max_active_runs:
                raise RunLimitExceeded("Active run limit reached", retry_after=30)
            hourly = connection.execute(
                "SELECT COUNT(*) AS n FROM niji_runs WHERE tenant_id=%s AND created_at >= %s",
                (tenant_id, hour_start),
            ).fetchone()["n"]
            if hourly >= max_submissions_per_hour:
                retry_after = max(1, int(hour_start + 3600 - now + 0.999))
                raise RunLimitExceeded(
                    "Hourly run submission limit reached", retry_after=retry_after
                )
            connection.execute(
                "INSERT INTO niji_runs (run_id, tenant_id, idempotency_key, fingerprint, "
                "request_json, timeout_seconds, status, created_at, updated_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (run_id, tenant_id, request.idempotency_key, request.fingerprint,
                 request._payload_json, request.timeout_seconds, RunStatus.QUEUED.value, now, now),
            )
            connection.execute(
                "INSERT INTO niji_run_events (run_id,status,timestamp,detail) VALUES (%s,%s,%s,%s)",
                (run_id, RunStatus.QUEUED.value, now, "Run accepted"),
            )
            if monthly_prompt_token_limit is not None:
                month = current_month_start(now)
                connection.execute(
                    "INSERT INTO niji_monthly_usage (tenant_id,month_start) VALUES (%s,%s) "
                    "ON CONFLICT (tenant_id,month_start) DO NOTHING", (tenant_id, month),
                )
                usage_row = connection.execute(
                    "SELECT * FROM niji_monthly_usage WHERE tenant_id=%s AND month_start=%s FOR UPDATE",
                    (tenant_id, month),
                ).fetchone()
                if (usage_row["prompt_tokens_used"] + usage_row["prompt_tokens_reserved"] + reserved_prompt_tokens
                        > monthly_prompt_token_limit
                        or usage_row["completion_tokens_used"] + usage_row["completion_tokens_reserved"] + reserved_completion_tokens
                        > monthly_completion_token_limit):
                    raise RunLimitExceeded("Monthly token budget reached",
                                           retry_after=seconds_until_month_end(now))
                if (monthly_cost_limit_micros is not None
                        and usage_row["cost_micros_used"] + usage_row["cost_micros_reserved"] + reserved_cost_micros
                        > monthly_cost_limit_micros):
                    raise RunLimitExceeded("Monthly spend budget reached",
                                           retry_after=seconds_until_month_end(now))
                connection.execute(
                    "UPDATE niji_monthly_usage SET prompt_tokens_reserved=prompt_tokens_reserved+%s,"
                    "completion_tokens_reserved=completion_tokens_reserved+%s,"
                    "cost_micros_reserved=cost_micros_reserved+%s WHERE tenant_id=%s AND month_start=%s",
                    (reserved_prompt_tokens, reserved_completion_tokens, reserved_cost_micros, tenant_id, month),
                )
                connection.execute(
                    "INSERT INTO niji_run_usage_reservations (run_id,tenant_id,month_start,reserved_prompt_tokens,"
                    "reserved_completion_tokens,reserved_cost_micros) VALUES (%s,%s,%s,%s,%s,%s)",
                    (run_id, tenant_id, month, reserved_prompt_tokens, reserved_completion_tokens,
                     reserved_cost_micros),
                )
            return self._snapshot(connection, tenant_id, run_id), True

    def get(self, tenant_id: str, run_id: str) -> RunSnapshot | None:
        self._validate_tenant(tenant_id)
        self._validate_run_id(run_id)
        with self._connect() as connection:
            return self._snapshot(connection, tenant_id, run_id)

    def get_request(self, tenant_id: str, run_id: str) -> RunRequest | None:
        self._validate_tenant(tenant_id)
        self._validate_run_id(run_id)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT idempotency_key, request_json, timeout_seconds FROM niji_runs "
                "WHERE tenant_id=%s AND run_id=%s", (tenant_id, run_id),
            ).fetchone()
        if row is None:
            return None
        return RunRequest(row["idempotency_key"], json.loads(row["request_json"]),
                          row["timeout_seconds"])

    @staticmethod
    def _validate_artifact_path(path: str) -> str:
        if (not isinstance(path, str) or not path or len(path) > 240
                or chr(0) in path or chr(92) in path or path.startswith("/")
                or (len(path) > 1 and path[1] == ":")):
            raise ValueError("Invalid artifact path")
        pure = PurePosixPath(path)
        if pure.is_absolute() or not pure.parts or any(part in ("", ".", "..") for part in pure.parts):
            raise ValueError("Invalid artifact path")
        normalized = pure.as_posix()
        names = [part.casefold() for part in pure.parts]
        basename = names[-1]
        if (any(name in _BLOCKED_ARTIFACT_DIRS for name in names)
                or basename in _BLOCKED_ARTIFACT_NAMES
                or (basename.startswith(".env") and basename != ".env.example")
                or basename.endswith((".pem", ".key", ".p12", ".pfx"))):
            raise ValueError("Sensitive and generated artifacts cannot be stored")
        return normalized

    def add_artifact(
        self, tenant_id: str, run_id: str, lease_token: str, path: str,
        content: bytes, content_type: str,
    ) -> StoredArtifact:
        """Persist one bounded artifact only while the matching run lease is live."""
        self._validate_tenant(tenant_id)
        self._validate_run_id(run_id)
        normalized = self._validate_artifact_path(path)
        if not isinstance(lease_token, str) or not lease_token:
            raise ValueError("lease_token must be non-empty text")
        if not isinstance(content, (bytes, bytearray, memoryview)):
            raise TypeError("artifact content must be bytes")
        data = bytes(content)
        if len(data) > _MAX_ARTIFACT_BYTES:
            raise ValueError("artifact exceeds the 1 MB per-file limit")
        if (not isinstance(content_type, str) or len(content_type) > 127
                or not re.fullmatch(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+", content_type)):
            raise ValueError("Invalid artifact content type")
        now = time.time()
        token_hash = hashlib.sha256(lease_token.encode("utf-8")).hexdigest()
        digest = hashlib.sha256(data).hexdigest()
        artifact_id = uuid.uuid4().hex
        with self._connect() as connection:
            run = connection.execute(
                "SELECT status,lease_token_hash,lease_expires_at,attempt_count FROM niji_runs "
                "WHERE tenant_id=%s AND run_id=%s FOR UPDATE", (tenant_id, run_id),
            ).fetchone()
            if (run is None or run["status"] != RunStatus.RUNNING.value
                    or not isinstance(run["lease_token_hash"], str)
                    or not hmac.compare_digest(run["lease_token_hash"], token_hash)
                    or run["lease_expires_at"] is None or run["lease_expires_at"] <= now):
                raise RunError("Worker lease is invalid or expired")
            previous = connection.execute(
                "SELECT size_bytes FROM niji_cloud_artifacts "
                "WHERE tenant_id=%s AND run_id=%s AND path=%s",
                (tenant_id, run_id, normalized),
            ).fetchone()
            stats = connection.execute(
                "SELECT COUNT(*) AS items,COALESCE(SUM(size_bytes),0) AS total "
                "FROM niji_cloud_artifacts WHERE tenant_id=%s AND run_id=%s",
                (tenant_id, run_id),
            ).fetchone()
            if previous is None and stats["items"] >= _MAX_ARTIFACTS_PER_RUN:
                raise ValueError("run artifact count limit exceeded")
            total = stats["total"] - (previous["size_bytes"] if previous else 0) + len(data)
            if total > _MAX_ARTIFACT_TOTAL_BYTES:
                raise ValueError("run artifact storage limit exceeded")
            connection.execute(
                "INSERT INTO niji_cloud_artifacts "
                "(artifact_id,tenant_id,run_id,path,content_type,sha256,size_bytes,content,created_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (tenant_id,run_id,path) DO UPDATE SET "
                "artifact_id=EXCLUDED.artifact_id,content_type=EXCLUDED.content_type,"
                "sha256=EXCLUDED.sha256,size_bytes=EXCLUDED.size_bytes,"
                "content=EXCLUDED.content,created_at=EXCLUDED.created_at",
                (artifact_id, tenant_id, run_id, normalized, content_type, digest, len(data), data, now),
            )
        return StoredArtifact(artifact_id, normalized, content_type, digest, len(data), now)

    def list_artifacts(self, tenant_id: str, run_id: str) -> list[StoredArtifact]:
        self._validate_tenant(tenant_id)
        self._validate_run_id(run_id)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT a.artifact_id,a.path,a.content_type,a.sha256,a.size_bytes,a.created_at "
                "FROM niji_cloud_artifacts a JOIN niji_runs r ON r.run_id=a.run_id "
                "WHERE a.tenant_id=%s AND a.run_id=%s AND r.status=%s ORDER BY a.path",
                (tenant_id, run_id, RunStatus.COMPLETED.value),
            ).fetchall()
        return [StoredArtifact(row["artifact_id"], row["path"], row["content_type"],
                               row["sha256"], row["size_bytes"], row["created_at"])
                for row in rows]

    def get_artifact(self, tenant_id: str, run_id: str, artifact_id: str) -> StoredArtifact | None:
        self._validate_tenant(tenant_id)
        self._validate_run_id(run_id)
        if not isinstance(artifact_id, str) or not re.fullmatch(r"[0-9a-f]{32}", artifact_id):
            return None
        with self._connect() as connection:
            row = connection.execute(
                "SELECT a.artifact_id,a.path,a.content_type,a.sha256,a.size_bytes,a.created_at,a.content "
                "FROM niji_cloud_artifacts a JOIN niji_runs r ON r.run_id=a.run_id "
                "WHERE a.tenant_id=%s AND a.run_id=%s AND a.artifact_id=%s AND r.status=%s",
                (tenant_id, run_id, artifact_id, RunStatus.COMPLETED.value),
            ).fetchone()
        if row is None:
            return None
        return StoredArtifact(row["artifact_id"], row["path"], row["content_type"],
                              row["sha256"], row["size_bytes"], row["created_at"], bytes(row["content"]))

    def cleanup_expired_artifacts(self, *, retention_days: int = 7) -> int:
        """Delete expired artifacts for completed runs in bounded batches.

        Artifacts owned by active runs are retained even if the run outlives the
        configured retention window. At most 10,000 rows are deleted per call so
        a large backlog cannot create one unbounded transaction.
        """
        if (isinstance(retention_days, bool) or not isinstance(retention_days, int)
                or not 1 <= retention_days <= 90):
            raise ValueError("retention_days must be between 1 and 90")
        cutoff = time.time() - retention_days * 24 * 60 * 60
        total_deleted = 0
        with self._connect() as connection:
            for _ in range(10):
                cursor = connection.execute(
                    "DELETE FROM niji_cloud_artifacts WHERE ctid IN ("
                    "SELECT a.ctid FROM niji_cloud_artifacts a "
                    "JOIN niji_runs r ON r.run_id=a.run_id "
                    "WHERE a.created_at < %s AND r.status=%s "
                    "ORDER BY a.created_at LIMIT 1000)",
                    (cutoff, RunStatus.COMPLETED.value),
                )
                total_deleted += max(cursor.rowcount, 0)
                if cursor.rowcount < 1000:
                    break
        return total_deleted

    @staticmethod
    def _validate_lease_options(lease_seconds: float, max_attempts: int | None = None) -> None:
        if (isinstance(lease_seconds, bool) or not isinstance(lease_seconds, (int, float))
                or not math.isfinite(float(lease_seconds)) or not 1 <= lease_seconds <= 3600):
            raise ValueError("lease_seconds must be between 1 and 3600")
        if max_attempts is not None and (
            isinstance(max_attempts, bool) or not isinstance(max_attempts, int)
            or not 1 <= max_attempts <= 10
        ):
            raise ValueError("max_attempts must be between 1 and 10")

    def claim_next(
        self, tenant_id: str | None, *, lease_seconds: float = 60, max_attempts: int = 3,
    ) -> RunClaim | None:
        if tenant_id is not None:
            self._validate_tenant(tenant_id)
        self._validate_lease_options(lease_seconds, max_attempts)
        now = time.time()
        with self._connect() as connection:
            scope = "" if tenant_id is None else "tenant_id=%s AND "
            params = (
                (RunStatus.QUEUED.value, RunStatus.RUNNING.value,
                 RunStatus.CANCELLING.value, now)
                if tenant_id is None else
                (tenant_id, RunStatus.QUEUED.value, RunStatus.RUNNING.value,
                 RunStatus.CANCELLING.value, now)
            )
            row = connection.execute(
                f"SELECT tenant_id,run_id,status,idempotency_key,request_json,timeout_seconds,attempt_count "
                f"FROM niji_runs WHERE {scope}(status=%s OR "
                "(status IN (%s,%s) AND (lease_expires_at IS NULL OR lease_expires_at<=%s))) "
                "ORDER BY created_at,run_id LIMIT 1 FOR UPDATE SKIP LOCKED",
                params,
            ).fetchone()
            if row is None:
                return None
            owner_id = row["tenant_id"]
            run_id = row["run_id"]
            status = RunStatus(row["status"])
            attempt = row["attempt_count"]
            if status == RunStatus.CANCELLING:
                target, detail, error = RunStatus.CANCELLED, "Cancelled after worker lease expired", None
                connection.execute(
                    "UPDATE niji_runs SET status=%s,updated_at=%s,lease_token_hash=NULL,"
                    "lease_expires_at=NULL WHERE tenant_id=%s AND run_id=%s",
                    (target.value, now, owner_id, run_id),
                )
                connection.execute(
                    "INSERT INTO niji_run_events (run_id,status,timestamp,detail) VALUES (%s,%s,%s,%s)",
                    (run_id, target.value, now, detail),
                )
                self._settle_usage(connection, run_id, None, attempt)
                return None
            if status == RunStatus.RUNNING and attempt >= max_attempts:
                detail = "Worker lease expired too many times"
                connection.execute(
                    "UPDATE niji_runs SET status=%s,updated_at=%s,error=%s,lease_token_hash=NULL,"
                    "lease_expires_at=NULL WHERE tenant_id=%s AND run_id=%s",
                    (RunStatus.FAILED.value, now, detail, owner_id, run_id),
                )
                connection.execute(
                    "INSERT INTO niji_run_events (run_id,status,timestamp,detail) VALUES (%s,%s,%s,%s)",
                    (run_id, RunStatus.FAILED.value, now, detail),
                )
                self._settle_usage(connection, run_id, None, attempt)
                return None
            attempt += 1
            # Discard files from expired attempts before the replacement starts.
            connection.execute(
                "DELETE FROM niji_cloud_artifacts WHERE tenant_id=%s AND run_id=%s",
                (owner_id, run_id),
            )
            token = secrets.token_urlsafe(32)
            expires = now + float(lease_seconds)
            connection.execute(
                "UPDATE niji_runs SET status=%s,updated_at=%s,lease_token_hash=%s,"
                "lease_expires_at=%s,attempt_count=%s,result_json=NULL,error=NULL "
                "WHERE tenant_id=%s AND run_id=%s",
                (RunStatus.RUNNING.value, now, hashlib.sha256(token.encode()).hexdigest(),
                 expires, attempt, owner_id, run_id),
            )
            connection.execute(
                "INSERT INTO niji_run_events (run_id,status,timestamp,detail) VALUES (%s,%s,%s,%s)",
                (run_id, RunStatus.RUNNING.value, now, f"Worker lease acquired (attempt {attempt})"),
            )
            request = RunRequest(row["idempotency_key"], json.loads(row["request_json"]),
                                 row["timeout_seconds"])
            return RunClaim(run_id, request, token, attempt, expires, owner_id)

    def renew_lease(self, tenant_id: str, run_id: str, lease_token: str, *, lease_seconds: float = 60) -> bool:
        self._validate_tenant(tenant_id)
        self._validate_run_id(run_id)
        if not isinstance(lease_token, str) or not lease_token:
            raise ValueError("lease_token must be non-empty text")
        self._validate_lease_options(lease_seconds)
        now = time.time()
        token_hash = hashlib.sha256(lease_token.encode()).hexdigest()
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE niji_runs SET lease_expires_at=%s,updated_at=%s WHERE tenant_id=%s "
                "AND run_id=%s AND status IN (%s,%s) AND lease_token_hash=%s AND lease_expires_at>%s",
                (now + float(lease_seconds), now, tenant_id, run_id,
                 RunStatus.RUNNING.value, RunStatus.CANCELLING.value, token_hash, now),
            )
            return cursor.rowcount == 1

    def record_progress(self, tenant_id: str, run_id: str, lease_token: str, detail: str) -> RunSnapshot:
        """Append one bounded, lease-owned RUNNING progress event."""
        self._validate_tenant(tenant_id)
        self._validate_run_id(run_id)
        safe_details = {"Agent is thinking", "Agent is working", "Agent is planning", "Response ready"}
        if not isinstance(detail, str) or detail not in safe_details:
            raise ValueError("Unsupported progress label")
        if not isinstance(lease_token, str) or not lease_token:
            raise ValueError("lease_token must be non-empty text")
        now = time.time()
        token_hash = hashlib.sha256(lease_token.encode("utf-8")).hexdigest()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT status,lease_token_hash,lease_expires_at,attempt_count FROM niji_runs "
                "WHERE tenant_id=%s AND run_id=%s FOR UPDATE", (tenant_id, run_id),
            ).fetchone()
            if (row is None or row["status"] != RunStatus.RUNNING.value
                    or row["lease_token_hash"] != token_hash
                    or row["lease_expires_at"] is None or row["lease_expires_at"] <= now):
                raise RunError("Worker lease is invalid or expired")
            connection.execute(
                "UPDATE niji_runs SET updated_at=%s WHERE tenant_id=%s AND run_id=%s",
                (now, tenant_id, run_id),
            )
            connection.execute(
                "INSERT INTO niji_run_events (run_id,status,timestamp,detail) VALUES (%s,%s,%s,%s)",
                (run_id, RunStatus.RUNNING.value, now, detail),
            )
            connection.execute(
                "DELETE FROM niji_run_events WHERE run_id=%s AND event_id NOT IN "
                "(SELECT event_id FROM niji_run_events WHERE run_id=%s ORDER BY event_id DESC LIMIT 200)",
                (run_id, run_id),
            )
            return self._snapshot(connection, tenant_id, run_id)

    def finish_claim(
        self, tenant_id: str, run_id: str, lease_token: str, target: RunStatus, detail: str,
        *, result: Any = None, error: str | None = None,
        usage: dict[str, Any] | None = None,
    ) -> RunSnapshot:
        if target not in _TERMINAL:
            raise ValueError("target must be a terminal run status")
        return self.transition(tenant_id, run_id, target, detail, result=result,
                               error=error, lease_token=lease_token, usage=usage)

    @staticmethod
    def _settle_usage(connection, run_id: str, usage: dict[str, Any] | None,
                      attempt_count: int) -> None:
        reservation = connection.execute(
            "SELECT * FROM niji_run_usage_reservations WHERE run_id=%s AND settled=FALSE FOR UPDATE",
            (run_id,),
        ).fetchone()
        if reservation is None:
            return
        rp, rc, rcost = (reservation["reserved_prompt_tokens"],
                         reservation["reserved_completion_tokens"],
                         reservation["reserved_cost_micros"])
        reported = (isinstance(usage, dict) and usage.get("usage_reported") is True
                    and usage.get("usage_complete") is True)
        if reported:
            try:
                ap = validate_token_count(usage.get("prompt_tokens"), "prompt_tokens")
                ac = validate_token_count(usage.get("completion_tokens"), "completion_tokens")
                acost = validate_cost_micros(usage.get("cost_micros", 0), "cost_micros")
                if ap > rp or ac > rc or acost > rcost:
                    reported = False
            except (ValueError, TypeError):
                reported = False
        if not reported:
            ap, ac, acost = (rp, rc, rcost) if attempt_count > 0 else (0, 0, 0)
        connection.execute(
            "UPDATE niji_monthly_usage SET prompt_tokens_reserved=GREATEST(0,prompt_tokens_reserved-%s),"
            "completion_tokens_reserved=GREATEST(0,completion_tokens_reserved-%s),"
            "cost_micros_reserved=GREATEST(0,cost_micros_reserved-%s),"
            "prompt_tokens_used=prompt_tokens_used+%s,completion_tokens_used=completion_tokens_used+%s,"
            "cost_micros_used=cost_micros_used+%s WHERE tenant_id=%s AND month_start=%s",
            (rp, rc, rcost, ap, ac, acost, reservation["tenant_id"], reservation["month_start"]),
        )
        connection.execute(
            "UPDATE niji_run_usage_reservations SET actual_prompt_tokens=%s,actual_completion_tokens=%s,"
            "actual_cost_micros=%s,settled=TRUE WHERE run_id=%s AND settled=FALSE",
            (ap, ac, acost, run_id),
        )

    def delete_tenant_data(self, tenant_id: str) -> int:
        """Permanently delete all stored run data for one inactive tenant."""
        self._validate_tenant(tenant_id)
        tenant_bucket = hashlib.sha256(("tenant:" + tenant_id).encode("utf-8")).hexdigest()
        with self._connect() as connection:
            # Serialize deletion with run admission so a simultaneous submission
            # cannot be accepted into a tenant partition being purged.
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (tenant_id,)
            )
            active = connection.execute(
                "SELECT run_id FROM niji_runs WHERE tenant_id=%s AND status IN (%s,%s,%s) "
                "LIMIT 1 FOR UPDATE",
                (tenant_id, RunStatus.QUEUED.value, RunStatus.RUNNING.value,
                 RunStatus.CANCELLING.value),
            ).fetchone()
            if active is not None:
                raise ActiveRunsPreventDeletion(
                    "Cancel or wait for active runs before deleting stored data"
                )
            # Delete artifacts explicitly as defense in depth; current schemas also
            # cascade them with their parent run records.
            connection.execute(
                "DELETE FROM niji_cloud_artifacts WHERE tenant_id=%s", (tenant_id,)
            )
            cursor = connection.execute(
                "DELETE FROM niji_runs WHERE tenant_id=%s", (tenant_id,)
            )
            deleted = cursor.rowcount
            connection.execute(
                "DELETE FROM niji_monthly_usage WHERE tenant_id=%s", (tenant_id,)
            )
            connection.execute(
                "DELETE FROM niji_rate_limit_buckets WHERE bucket_key=%s", (tenant_bucket,)
            )
            return max(0, int(deleted))

    def list_recent(self, tenant_id: str, *, limit: int = 20) -> list[RunSnapshot]:
        self._validate_tenant(tenant_id)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= _MAX_LIST_LIMIT:
            raise ValueError(f"limit must be between 1 and {_MAX_LIST_LIMIT}")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT run_id FROM niji_runs WHERE tenant_id=%s ORDER BY updated_at DESC,run_id DESC LIMIT %s",
                (tenant_id, limit),
            ).fetchall()
            return [self._snapshot(connection, tenant_id, row["run_id"]) for row in rows]

    def transition(
        self, tenant_id: str, run_id: str, target: RunStatus, detail: str, *,
        result: Any = None, error: str | None = None, lease_token: str | None = None,
        usage: dict[str, Any] | None = None,
    ) -> RunSnapshot:
        self._validate_tenant(tenant_id)
        self._validate_run_id(run_id)
        if not isinstance(target, RunStatus):
            raise TypeError("target must be a RunStatus")
        if not isinstance(detail, str):
            raise TypeError("detail must be text")
        if error is not None and not isinstance(error, str):
            raise TypeError("error must be text or None")
        result_json = None
        if target == RunStatus.COMPLETED:
            try:
                result_json = json.dumps(result, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            except (TypeError, ValueError) as exc:
                raise ValueError("result must contain only JSON-safe values") from exc
            if len(result_json.encode("utf-8")) > _MAX_RESULT_BYTES:
                raise ValueError("result exceeds the safe 250 KB limit")
        if target not in _TERMINAL and (result is not None or error is not None):
            raise ValueError("Only terminal transitions can store a result or error")
        clean_error = error[:_MAX_ERROR_LENGTH] if error is not None else None
        now = time.time()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT status,lease_token_hash,lease_expires_at,attempt_count FROM niji_runs "
                "WHERE tenant_id=%s AND run_id=%s FOR UPDATE", (tenant_id, run_id),
            ).fetchone()
            if row is None:
                raise KeyError("Unknown run id")
            current = RunStatus(row["status"])
            token_hash = row["lease_token_hash"]
            if token_hash is not None and target in _TERMINAL:
                supplied = hashlib.sha256(lease_token.encode()).hexdigest() if isinstance(lease_token, str) else None
                if supplied != token_hash or row["lease_expires_at"] is None or row["lease_expires_at"] <= now:
                    raise RunError("Worker lease is invalid or expired")
            elif token_hash is None and lease_token is not None:
                raise RunError("Worker lease is no longer active")
            if current == RunStatus.CANCELLING and target == RunStatus.COMPLETED:
                target, detail, result_json, clean_error = RunStatus.CANCELLED, "Run cancelled", None, None
            if target not in _ALLOWED_TRANSITIONS.get(current, set()):
                raise RunError(f"Invalid lifecycle transition: {current.value} -> {target.value}")
            terminal = target in _TERMINAL
            connection.execute(
                "UPDATE niji_runs SET status=%s,updated_at=%s,result_json=%s,error=%s,"
                "lease_token_hash=%s,lease_expires_at=%s WHERE tenant_id=%s AND run_id=%s",
                (target.value, now, result_json if target == RunStatus.COMPLETED else None,
                 clean_error, None if terminal else token_hash,
                 None if terminal else row["lease_expires_at"], tenant_id, run_id),
            )
            if target != RunStatus.COMPLETED:
                connection.execute(
                    "DELETE FROM niji_cloud_artifacts WHERE tenant_id=%s AND run_id=%s",
                    (tenant_id, run_id),
                )
            connection.execute(
                "INSERT INTO niji_run_events (run_id,status,timestamp,detail) VALUES (%s,%s,%s,%s)",
                (run_id, target.value, now, detail[:200]),
            )
            if terminal:
                self._settle_usage(connection, run_id, usage, row["attempt_count"])
            snapshot = self._snapshot(connection, tenant_id, run_id)
            return snapshot
