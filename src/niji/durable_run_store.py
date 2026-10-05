"""Crash-durable, tenant-scoped run records for single-node cloud adapters.

This is persistence, not authentication or distributed coordination. Callers must
supply a tenant identity established by a trusted auth layer. SQLite is suitable
for local development and a single-host deployment; multi-host deployments should
provide the same repository contract on a managed database.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .cloud_usage import (current_month_start, seconds_until_month_end,
                          validate_cost_micros, validate_token_count)
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

_SCHEMA_VERSION = 7
_TENANT_ID = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")
_RUN_ID = re.compile(r"^[a-f0-9]{32}$")
_RATE_LIMIT_KEY = re.compile(r"^[a-f0-9]{64}$")
_MAX_LIST_LIMIT = 100


def _ensure_rate_limit_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """CREATE TABLE IF NOT EXISTS rate_limit_buckets (
            bucket_key TEXT NOT NULL,
            window_start INTEGER NOT NULL,
            window_end INTEGER NOT NULL,
            request_count INTEGER NOT NULL CHECK (request_count >= 0),
            PRIMARY KEY (bucket_key, window_start)
        )"""
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS rate_limit_buckets_by_window "
        "ON rate_limit_buckets (window_end)"
    )


@dataclass(frozen=True)
class RunClaim:
    """A worker's short-lived, unguessable lease on one queued run."""

    run_id: str
    request: RunRequest
    lease_token: str
    attempt: int
    lease_expires_at: float
    tenant_id: str = ""

_ALLOWED_TRANSITIONS = {
    RunStatus.QUEUED: {RunStatus.RUNNING, RunStatus.CANCELLING, RunStatus.CANCELLED},
    RunStatus.RUNNING: {
        RunStatus.CANCELLING, RunStatus.COMPLETED, RunStatus.CANCELLED,
        RunStatus.TIMED_OUT, RunStatus.FAILED,
    },
    RunStatus.CANCELLING: {RunStatus.CANCELLED, RunStatus.TIMED_OUT, RunStatus.FAILED},
}


class SQLiteRunStore:
    """Persist cloud run requests, status, results, and events in SQLite.

    Idempotency keys are unique per tenant. Reads and mutations require the tenant
    key on every call, so an adapter cannot accidentally fetch another tenant's run
    by ID. This is a storage guard, not proof of tenant identity: authenticate that
    identity before calling this class.
    """

    def __init__(self, database_path: str | Path):
        self.database_path = Path(database_path).expanduser()
        parent = self.database_path.parent
        if parent.is_symlink():
            raise OSError("Run database directory must not be a symlink")
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.database_path.is_symlink():
            raise OSError("Run database must not be a symlink")
        if self.database_path.exists() and not self.database_path.is_file():
            raise OSError("Run database path must be a regular file")
        if not self.database_path.exists():
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
            fd = os.open(self.database_path, flags, 0o600)
            os.close(fd)
        if os.name == "posix":
            self.database_path.chmod(0o600)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        if self.database_path.is_symlink():
            raise OSError("Run database must not be a symlink")
        connection = sqlite3.connect(self.database_path, timeout=5.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def healthcheck(self) -> bool:
        """Return whether the SQLite database can answer a lightweight query."""
        connection = self._connect()
        try:
            row = connection.execute("SELECT 1").fetchone()
            return row is not None and row[0] == 1
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version > _SCHEMA_VERSION:
                raise RunError("Run database was created by a newer Niji version")
            if version == 0:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    connection.execute(
                        """CREATE TABLE runs (
                            run_id TEXT PRIMARY KEY,
                            tenant_id TEXT NOT NULL,
                            idempotency_key TEXT NOT NULL,
                            fingerprint TEXT NOT NULL,
                            request_json TEXT NOT NULL,
                            timeout_seconds REAL NOT NULL,
                            status TEXT NOT NULL,
                            created_at REAL NOT NULL,
                            updated_at REAL NOT NULL,
                            result_json TEXT,
                            error TEXT,
                            lease_token_hash TEXT,
                            lease_expires_at REAL,
                            attempt_count INTEGER NOT NULL DEFAULT 0,
                            UNIQUE (tenant_id, idempotency_key)
                        )"""
                    )
                    connection.execute(
                        "CREATE INDEX runs_by_tenant_updated ON runs (tenant_id, updated_at DESC)"
                    )
                    connection.execute(
                        "CREATE INDEX runs_by_tenant_created ON runs (tenant_id, created_at)"
                    )
                    connection.execute(
                        "CREATE INDEX runs_by_tenant_status ON runs (tenant_id, status)"
                    )
                    connection.execute(
                        "CREATE INDEX runs_by_status_created ON runs (status, created_at, run_id)"
                    )
                    connection.execute(
                        """CREATE TABLE run_events (
                            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                            run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                            status TEXT NOT NULL,
                            timestamp REAL NOT NULL,
                            detail TEXT NOT NULL
                        )"""
                    )
                    _ensure_rate_limit_schema(connection)
                    connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION - 1}")
                    connection.execute("COMMIT")
                except Exception:
                    connection.execute("ROLLBACK")
                    raise
            elif version == 1:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    connection.execute("ALTER TABLE runs ADD COLUMN lease_token_hash TEXT")
                    connection.execute("ALTER TABLE runs ADD COLUMN lease_expires_at REAL")
                    connection.execute(
                        "ALTER TABLE runs ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0"
                    )
                    # Runs started by the earlier preview have no lease and are eligible
                    # for one safe recovery claim after upgrade.
                    connection.execute(
                        "UPDATE runs SET lease_expires_at=0 WHERE status IN ('running', 'cancelling')"
                    )
                    connection.execute(
                        "CREATE INDEX IF NOT EXISTS runs_by_tenant_created ON runs (tenant_id, created_at)"
                    )
                    connection.execute(
                        "CREATE INDEX IF NOT EXISTS runs_by_tenant_status ON runs (tenant_id, status)"
                    )
                    connection.execute(
                        "CREATE INDEX IF NOT EXISTS runs_by_status_created ON runs (status, created_at, run_id)"
                    )
                    _ensure_rate_limit_schema(connection)
                    connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION - 1}")
                    connection.execute("COMMIT")
                except Exception:
                    connection.execute("ROLLBACK")
                    raise
            elif version == 2:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    connection.execute(
                        "CREATE INDEX IF NOT EXISTS runs_by_tenant_created ON runs (tenant_id, created_at)"
                    )
                    connection.execute(
                        "CREATE INDEX IF NOT EXISTS runs_by_tenant_status ON runs (tenant_id, status)"
                    )
                    connection.execute(
                        "CREATE INDEX IF NOT EXISTS runs_by_status_created ON runs (status, created_at, run_id)"
                    )
                    _ensure_rate_limit_schema(connection)
                    connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION - 1}")
                    connection.execute("COMMIT")
                except Exception:
                    connection.execute("ROLLBACK")
                    raise
            elif version == 3:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    connection.execute(
                        "CREATE INDEX IF NOT EXISTS runs_by_status_created ON runs (status, created_at, run_id)"
                    )
                    _ensure_rate_limit_schema(connection)
                    connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION - 1}")
                    connection.execute("COMMIT")
                except Exception:
                    connection.execute("ROLLBACK")
                    raise
            elif version == 4:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    # v4 databases already have the run/event schema; ensure all
                    # indexes expected by the current store before advancing.
                    connection.execute(
                        "CREATE INDEX IF NOT EXISTS runs_by_tenant_created ON runs (tenant_id, created_at)"
                    )
                    connection.execute(
                        "CREATE INDEX IF NOT EXISTS runs_by_tenant_status ON runs (tenant_id, status)"
                    )
                    connection.execute(
                        "CREATE INDEX IF NOT EXISTS runs_by_status_created ON runs (status, created_at, run_id)"
                    )
                    _ensure_rate_limit_schema(connection)
                    connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION - 1}")
                    connection.execute("COMMIT")
                except Exception:
                    connection.execute("ROLLBACK")
                    raise
            elif version == 5:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    _ensure_rate_limit_schema(connection)
                    connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION - 1}")
                    connection.execute("COMMIT")
                except Exception:
                    connection.execute("ROLLBACK")
                    raise
            if version < 7:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    connection.execute(
                        """CREATE TABLE IF NOT EXISTS monthly_usage (
                            tenant_id TEXT NOT NULL,
                            month_start INTEGER NOT NULL,
                            prompt_tokens_used INTEGER NOT NULL DEFAULT 0 CHECK (prompt_tokens_used >= 0),
                            completion_tokens_used INTEGER NOT NULL DEFAULT 0 CHECK (completion_tokens_used >= 0),
                            prompt_tokens_reserved INTEGER NOT NULL DEFAULT 0 CHECK (prompt_tokens_reserved >= 0),
                            completion_tokens_reserved INTEGER NOT NULL DEFAULT 0 CHECK (completion_tokens_reserved >= 0),
                            cost_micros_used INTEGER NOT NULL DEFAULT 0 CHECK (cost_micros_used >= 0),
                            cost_micros_reserved INTEGER NOT NULL DEFAULT 0 CHECK (cost_micros_reserved >= 0),
                            PRIMARY KEY (tenant_id, month_start)
                        )"""
                    )
                    connection.execute(
                        """CREATE TABLE IF NOT EXISTS run_usage_reservations (
                            run_id TEXT PRIMARY KEY REFERENCES runs(run_id) ON DELETE CASCADE,
                            tenant_id TEXT NOT NULL,
                            month_start INTEGER NOT NULL,
                            reserved_prompt_tokens INTEGER NOT NULL CHECK (reserved_prompt_tokens >= 0),
                            reserved_completion_tokens INTEGER NOT NULL CHECK (reserved_completion_tokens >= 0),
                            reserved_cost_micros INTEGER NOT NULL DEFAULT 0 CHECK (reserved_cost_micros >= 0),
                            actual_prompt_tokens INTEGER,
                            actual_completion_tokens INTEGER,
                            actual_cost_micros INTEGER,
                            settled INTEGER NOT NULL DEFAULT 0 CHECK (settled IN (0, 1))
                        )"""
                    )
                    connection.execute(
                        "CREATE INDEX IF NOT EXISTS monthly_usage_by_month ON monthly_usage (month_start)"
                    )
                    connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
                    connection.execute("COMMIT")
                except Exception:
                    if connection.in_transaction:
                        connection.execute("ROLLBACK")
                    raise
        if os.name == "posix":
            self.database_path.chmod(0o600)

    @staticmethod
    def _validate_tenant(tenant_id: str) -> None:
        if not isinstance(tenant_id, str) or not _TENANT_ID.fullmatch(tenant_id):
            raise ValueError("tenant_id must be 1-200 safe characters")

    @staticmethod
    def _validate_run_id(run_id: str) -> None:
        if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
            raise ValueError("Invalid run id")

    def consume_rate_limit(
        self, bucket_key: str, *, limit: int, window_seconds: int = 60,
    ) -> int | None:
        """Atomically consume one hashed request bucket; return retry seconds if blocked."""
        if not isinstance(bucket_key, str) or not _RATE_LIMIT_KEY.fullmatch(bucket_key):
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
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "DELETE FROM rate_limit_buckets WHERE window_end<=?", (int(now),)
                )
                connection.execute(
                    "INSERT INTO rate_limit_buckets (bucket_key,window_start,window_end,request_count) "
                    "VALUES (?,?,?,1) ON CONFLICT (bucket_key,window_start) DO UPDATE SET "
                    "request_count=MIN(rate_limit_buckets.request_count+1,?)",
                    (bucket_key, window_start, window_end, limit + 1),
                )
                row = connection.execute(
                    "SELECT request_count FROM rate_limit_buckets WHERE bucket_key=? AND window_start=?",
                    (bucket_key, window_start),
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
        if row["request_count"] <= limit:
            return None
        return max(1, window_end - int(now))

    def create(self, tenant_id: str, request: RunRequest) -> tuple[RunSnapshot, bool]:
        """Create a queued run, or return an equivalent idempotent retry.

        Returns ``(snapshot, created)``; ``created`` is false when an equivalent
        request already exists for this tenant and idempotency key.
        """
        self._validate_tenant(tenant_id)
        if not isinstance(request, RunRequest):
            raise TypeError("request must be a RunRequest")
        run_id = uuid.uuid4().hex
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT run_id, fingerprint FROM runs WHERE tenant_id=? AND idempotency_key=?",
                    (tenant_id, request.idempotency_key),
                ).fetchone()
                if existing is not None:
                    if existing["fingerprint"] != request.fingerprint:
                        raise IdempotencyConflict(
                            "Idempotency key already belongs to a different request"
                        )
                    snapshot = self._snapshot(connection, tenant_id, existing["run_id"])
                    connection.execute("COMMIT")
                    return snapshot, False
                connection.execute(
                    """INSERT INTO runs (
                        run_id, tenant_id, idempotency_key, fingerprint, request_json,
                        timeout_seconds, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (run_id, tenant_id, request.idempotency_key, request.fingerprint,
                     request._payload_json, request.timeout_seconds,
                     RunStatus.QUEUED.value, now, now),
                )
                connection.execute(
                    "INSERT INTO run_events (run_id, status, timestamp, detail) VALUES (?, ?, ?, ?)",
                    (run_id, RunStatus.QUEUED.value, now, "Run accepted"),
                )
                snapshot = self._snapshot(connection, tenant_id, run_id)
                connection.execute("COMMIT")
                return snapshot, True
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    @staticmethod
    def _reserve_usage(connection, tenant_id: str, run_id: str, *, now: float,
                       monthly_prompt_token_limit: int | None,
                       monthly_completion_token_limit: int | None,
                       reserved_prompt_tokens: int, reserved_completion_tokens: int,
                       monthly_cost_limit_micros: int | None,
                       reserved_cost_micros: int) -> None:
        if monthly_prompt_token_limit is None:
            return
        month = current_month_start(now)
        connection.execute(
            "INSERT INTO monthly_usage (tenant_id,month_start) VALUES (?,?) "
            "ON CONFLICT (tenant_id,month_start) DO NOTHING", (tenant_id, month),
        )
        row = connection.execute(
            "SELECT * FROM monthly_usage WHERE tenant_id=? AND month_start=?",
            (tenant_id, month),
        ).fetchone()
        if (row["prompt_tokens_used"] + row["prompt_tokens_reserved"] + reserved_prompt_tokens
                > monthly_prompt_token_limit
                or row["completion_tokens_used"] + row["completion_tokens_reserved"] + reserved_completion_tokens
                > monthly_completion_token_limit):
            raise RunLimitExceeded("Monthly token budget reached",
                                   retry_after=seconds_until_month_end(now))
        if (monthly_cost_limit_micros is not None
                and row["cost_micros_used"] + row["cost_micros_reserved"] + reserved_cost_micros
                > monthly_cost_limit_micros):
            raise RunLimitExceeded("Monthly spend budget reached",
                                   retry_after=seconds_until_month_end(now))
        connection.execute(
            "UPDATE monthly_usage SET prompt_tokens_reserved=prompt_tokens_reserved+?,"
            "completion_tokens_reserved=completion_tokens_reserved+?,"
            "cost_micros_reserved=cost_micros_reserved+? WHERE tenant_id=? AND month_start=?",
            (reserved_prompt_tokens, reserved_completion_tokens, reserved_cost_micros,
             tenant_id, month),
        )
        connection.execute(
            "INSERT INTO run_usage_reservations (run_id,tenant_id,month_start,reserved_prompt_tokens,"
            "reserved_completion_tokens,reserved_cost_micros) VALUES (?,?,?,?,?,?)",
            (run_id, tenant_id, month, reserved_prompt_tokens, reserved_completion_tokens,
             reserved_cost_micros),
        )

    def create_limited(
        self, tenant_id: str, request: RunRequest, *,
        max_active_runs: int = 5, max_submissions_per_hour: int = 20,
        monthly_prompt_token_limit: int | None = None,
        monthly_completion_token_limit: int | None = None,
        reserved_prompt_tokens: int = 0, reserved_completion_tokens: int = 0,
        monthly_cost_limit_micros: int | None = None, reserved_cost_micros: int = 0,
    ) -> tuple[RunSnapshot, bool]:
        """Create a run under transactional per-tenant queue and hourly limits."""
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
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT run_id,fingerprint FROM runs WHERE tenant_id=? AND idempotency_key=?",
                    (tenant_id, request.idempotency_key),
                ).fetchone()
                if existing is not None:
                    if existing["fingerprint"] != request.fingerprint:
                        raise IdempotencyConflict(
                            "Idempotency key already belongs to a different request"
                        )
                    snapshot = self._snapshot(connection, tenant_id, existing["run_id"])
                    connection.execute("COMMIT")
                    return snapshot, False
                active = connection.execute(
                    "SELECT COUNT(*) FROM runs WHERE tenant_id=? AND status IN (?, ?, ?)",
                    (tenant_id, RunStatus.QUEUED.value, RunStatus.RUNNING.value,
                     RunStatus.CANCELLING.value),
                ).fetchone()[0]
                if active >= max_active_runs:
                    raise RunLimitExceeded("Active run limit reached", retry_after=30)
                hourly = connection.execute(
                    "SELECT COUNT(*) FROM runs WHERE tenant_id=? AND created_at>=?",
                    (tenant_id, hour_start),
                ).fetchone()[0]
                if hourly >= max_submissions_per_hour:
                    retry_after = max(1, int(hour_start + 3600 - now + 0.999))
                    raise RunLimitExceeded("Hourly run submission limit reached", retry_after=retry_after)
                connection.execute(
                    "INSERT INTO runs (run_id,tenant_id,idempotency_key,fingerprint,request_json,"
                    "timeout_seconds,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (run_id, tenant_id, request.idempotency_key, request.fingerprint,
                     request._payload_json, request.timeout_seconds, RunStatus.QUEUED.value, now, now),
                )
                connection.execute(
                    "INSERT INTO run_events (run_id,status,timestamp,detail) VALUES (?,?,?,?)",
                    (run_id, RunStatus.QUEUED.value, now, "Run accepted"),
                )
                self._reserve_usage(
                    connection, tenant_id, run_id, now=now,
                    monthly_prompt_token_limit=monthly_prompt_token_limit,
                    monthly_completion_token_limit=monthly_completion_token_limit,
                    reserved_prompt_tokens=reserved_prompt_tokens,
                    reserved_completion_tokens=reserved_completion_tokens,
                    monthly_cost_limit_micros=monthly_cost_limit_micros,
                    reserved_cost_micros=reserved_cost_micros,
                )
                snapshot = self._snapshot(connection, tenant_id, run_id)
                connection.execute("COMMIT")
                return snapshot, True
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def get(self, tenant_id: str, run_id: str) -> RunSnapshot | None:
        self._validate_tenant(tenant_id)
        self._validate_run_id(run_id)
        with self._connect() as connection:
            return self._snapshot(connection, tenant_id, run_id)

    def get_request(self, tenant_id: str, run_id: str) -> RunRequest | None:
        """Return the original request for a trusted worker recovering a run."""
        self._validate_tenant(tenant_id)
        self._validate_run_id(run_id)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT idempotency_key, request_json, timeout_seconds FROM runs "
                "WHERE tenant_id=? AND run_id=?", (tenant_id, run_id),
            ).fetchone()
        if row is None:
            return None
        return RunRequest(
            row["idempotency_key"], json.loads(row["request_json"]), row["timeout_seconds"]
        )

    def claim_next(
        self,
        tenant_id: str | None,
        *,
        lease_seconds: float = 60,
        max_attempts: int = 3,
    ) -> RunClaim | None:
        """Atomically lease the oldest eligible run to one background worker.

        Expired RUNNING leases can be reclaimed up to ``max_attempts``. The raw
        lease token is returned once and only its SHA-256 digest is stored. This
        coordinates workers on one SQLite host; it is not a multi-host database.
        """
        if tenant_id is not None:
            self._validate_tenant(tenant_id)
        if (isinstance(lease_seconds, bool) or not isinstance(lease_seconds, (int, float))
                or not math.isfinite(float(lease_seconds)) or not 1 <= lease_seconds <= 3600):
            raise ValueError("lease_seconds must be between 1 and 3600")
        if (isinstance(max_attempts, bool) or not isinstance(max_attempts, int)
                or not 1 <= max_attempts <= 10):
            raise ValueError("max_attempts must be between 1 and 10")
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                tenant_clause = "" if tenant_id is None else "tenant_id=? AND "
                params = (
                    (RunStatus.QUEUED.value, RunStatus.RUNNING.value,
                     RunStatus.CANCELLING.value, now)
                    if tenant_id is None else
                    (tenant_id, RunStatus.QUEUED.value, RunStatus.RUNNING.value,
                     RunStatus.CANCELLING.value, now)
                )
                candidate = connection.execute(
                    f"SELECT tenant_id, run_id, status, idempotency_key, request_json, timeout_seconds, attempt_count "
                    f"FROM runs WHERE {tenant_clause}(status=? OR "
                    "(status IN (?, ?) AND (lease_expires_at IS NULL OR lease_expires_at<=?))) "
                    "ORDER BY created_at, run_id LIMIT 1",
                    params,
                ).fetchone()
                if candidate is None:
                    connection.execute("COMMIT")
                    return None

                owner_id = candidate["tenant_id"]
                run_id = candidate["run_id"]
                status = RunStatus(candidate["status"])
                attempt = candidate["attempt_count"]
                if status == RunStatus.CANCELLING:
                    connection.execute(
                        "UPDATE runs SET status=?, updated_at=?, lease_token_hash=NULL, "
                        "lease_expires_at=NULL WHERE tenant_id=? AND run_id=?",
                        (RunStatus.CANCELLED.value, now, owner_id, run_id),
                    )
                    connection.execute(
                        "INSERT INTO run_events (run_id, status, timestamp, detail) VALUES (?, ?, ?, ?)",
                        (run_id, RunStatus.CANCELLED.value, now, "Cancelled after worker lease expired"),
                    )
                    self._settle_usage(connection, run_id, None, attempt)
                    connection.execute("COMMIT")
                    return None
                if status == RunStatus.RUNNING and attempt >= max_attempts:
                    message = "Worker lease expired too many times"
                    connection.execute(
                        "UPDATE runs SET status=?, updated_at=?, error=?, lease_token_hash=NULL, "
                        "lease_expires_at=NULL WHERE tenant_id=? AND run_id=?",
                        (RunStatus.FAILED.value, now, message, owner_id, run_id),
                    )
                    connection.execute(
                        "INSERT INTO run_events (run_id, status, timestamp, detail) VALUES (?, ?, ?, ?)",
                        (run_id, RunStatus.FAILED.value, now, message),
                    )
                    self._settle_usage(connection, run_id, None, attempt)
                    connection.execute("COMMIT")
                    return None

                attempt += 1
                token = secrets.token_urlsafe(32)
                token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
                expires_at = now + float(lease_seconds)
                connection.execute(
                    "UPDATE runs SET status=?, updated_at=?, lease_token_hash=?, "
                    "lease_expires_at=?, attempt_count=?, result_json=NULL, error=NULL "
                    "WHERE tenant_id=? AND run_id=?",
                    (RunStatus.RUNNING.value, now, token_hash, expires_at, attempt, owner_id, run_id),
                )
                connection.execute(
                    "INSERT INTO run_events (run_id, status, timestamp, detail) VALUES (?, ?, ?, ?)",
                    (run_id, RunStatus.RUNNING.value, now, f"Worker lease acquired (attempt {attempt})"),
                )
                request = RunRequest(
                    candidate["idempotency_key"],
                    json.loads(candidate["request_json"]),
                    candidate["timeout_seconds"],
                )
                connection.execute("COMMIT")
                return RunClaim(run_id, request, token, attempt, expires_at, owner_id)
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def renew_lease(
        self,
        tenant_id: str,
        run_id: str,
        lease_token: str,
        *,
        lease_seconds: float = 60,
    ) -> bool:
        """Extend an unexpired worker lease; stale or cancelled workers get false."""
        self._validate_tenant(tenant_id)
        self._validate_run_id(run_id)
        if not isinstance(lease_token, str) or not lease_token:
            raise ValueError("lease_token must be non-empty text")
        if (isinstance(lease_seconds, bool) or not isinstance(lease_seconds, (int, float))
                or not math.isfinite(float(lease_seconds)) or not 1 <= lease_seconds <= 3600):
            raise ValueError("lease_seconds must be between 1 and 3600")
        now = time.time()
        token_hash = hashlib.sha256(lease_token.encode("utf-8")).hexdigest()
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE runs SET lease_expires_at=?, updated_at=? WHERE tenant_id=? AND run_id=? "
                "AND status IN (?,?) AND lease_token_hash=? AND lease_expires_at>?",
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
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    "UPDATE runs SET updated_at=? WHERE tenant_id=? AND run_id=? AND status=? "
                    "AND lease_token_hash=? AND lease_expires_at>?",
                    (now, tenant_id, run_id, RunStatus.RUNNING.value, token_hash, now),
                )
                if cursor.rowcount != 1:
                    raise RunError("Worker lease is invalid or expired")
                connection.execute(
                    "INSERT INTO run_events (run_id,status,timestamp,detail) VALUES (?,?,?,?)",
                    (run_id, RunStatus.RUNNING.value, now, detail),
                )
                connection.execute(
                    "DELETE FROM run_events WHERE run_id=? AND event_id NOT IN "
                    "(SELECT event_id FROM run_events WHERE run_id=? ORDER BY event_id DESC LIMIT 200)",
                    (run_id, run_id),
                )
                snapshot = self._snapshot(connection, tenant_id, run_id)
                connection.execute("COMMIT")
                return snapshot
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def finish_claim(
        self,
        tenant_id: str,
        run_id: str,
        lease_token: str,
        target: RunStatus,
        detail: str,
        *,
        result: Any = None,
        error: str | None = None,
        usage: dict[str, Any] | None = None,
    ) -> RunSnapshot:
        """Finish a run only if this worker still owns a live lease."""
        if target not in _TERMINAL:
            raise ValueError("target must be a terminal run status")
        return self.transition(
            tenant_id, run_id, target, detail, result=result, error=error,
            lease_token=lease_token, usage=usage,
        )

    @staticmethod
    def _settle_usage(connection, run_id: str, usage: dict[str, Any] | None,
                      attempt_count: int) -> None:
        reservation = connection.execute(
            "SELECT * FROM run_usage_reservations WHERE run_id=? AND settled=0", (run_id,),
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
            "UPDATE monthly_usage SET prompt_tokens_reserved=MAX(0,prompt_tokens_reserved-?),"
            "completion_tokens_reserved=MAX(0,completion_tokens_reserved-?),"
            "cost_micros_reserved=MAX(0,cost_micros_reserved-?),"
            "prompt_tokens_used=prompt_tokens_used+?,completion_tokens_used=completion_tokens_used+?,"
            "cost_micros_used=cost_micros_used+? WHERE tenant_id=? AND month_start=?",
            (rp, rc, rcost, ap, ac, acost, reservation["tenant_id"], reservation["month_start"]),
        )
        connection.execute(
            "UPDATE run_usage_reservations SET actual_prompt_tokens=?,actual_completion_tokens=?,"
            "actual_cost_micros=?,settled=1 WHERE run_id=? AND settled=0",
            (ap, ac, acost, run_id),
        )

    def delete_tenant_data(self, tenant_id: str) -> int:
        """Permanently delete all stored run data for one inactive tenant.

        Active work blocks deletion so a worker cannot continue after its request
        data has been removed. Returns the number of run records deleted.
        """
        self._validate_tenant(tenant_id)
        tenant_bucket = hashlib.sha256(("tenant:" + tenant_id).encode("utf-8")).hexdigest()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                active = connection.execute(
                    "SELECT 1 FROM runs WHERE tenant_id=? AND status IN (?,?,?) LIMIT 1",
                    (tenant_id, RunStatus.QUEUED.value, RunStatus.RUNNING.value,
                     RunStatus.CANCELLING.value),
                ).fetchone()
                if active is not None:
                    raise ActiveRunsPreventDeletion(
                        "Cancel or wait for active runs before deleting stored data"
                    )
                deleted = connection.execute(
                    "DELETE FROM runs WHERE tenant_id=?", (tenant_id,)
                ).rowcount
                connection.execute(
                    "DELETE FROM monthly_usage WHERE tenant_id=?", (tenant_id,)
                )
                connection.execute(
                    "DELETE FROM rate_limit_buckets WHERE bucket_key=?", (tenant_bucket,)
                )
                connection.execute("COMMIT")
                return max(0, int(deleted))
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def list_recent(self, tenant_id: str, *, limit: int = 20) -> list[RunSnapshot]:
        self._validate_tenant(tenant_id)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= _MAX_LIST_LIMIT:
            raise ValueError(f"limit must be between 1 and {_MAX_LIST_LIMIT}")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT run_id FROM runs WHERE tenant_id=? "
                "ORDER BY updated_at DESC, run_id DESC LIMIT ?", (tenant_id, limit),
            ).fetchall()
            return [self._snapshot(connection, tenant_id, row["run_id"]) for row in rows]

    def transition(
        self,
        tenant_id: str,
        run_id: str,
        target: RunStatus,
        detail: str,
        *,
        result: Any = None,
        error: str | None = None,
        lease_token: str | None = None,
        usage: dict[str, Any] | None = None,
    ) -> RunSnapshot:
        """Atomically append a lifecycle event and update one tenant-owned run.

        Claimed runs require their live lease token for completion, preventing a
        stale worker from overwriting the result of a newer attempt.
        """
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
                result_json = json.dumps(
                    result, ensure_ascii=False, separators=(",", ":"), allow_nan=False
                )
            except (TypeError, ValueError) as exc:
                raise ValueError("result must contain only JSON-safe values") from exc
            if len(result_json.encode("utf-8")) > _MAX_RESULT_BYTES:
                raise ValueError("result exceeds the safe 250 KB limit")
        if target not in _TERMINAL and (result is not None or error is not None):
            raise ValueError("Only terminal transitions can store a result or error")
        clean_error = error[:_MAX_ERROR_LENGTH] if error is not None else None
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT status, lease_token_hash, lease_expires_at, attempt_count FROM runs "
                    "WHERE tenant_id=? AND run_id=?",
                    (tenant_id, run_id),
                ).fetchone()
                if row is None:
                    raise KeyError("Unknown run id")
                stored_token_hash = row["lease_token_hash"]
                current = RunStatus(row["status"])
                # API-initiated cancellation may mark a leased run CANCELLING without
                # owning its worker token. Only terminal worker results require it.
                if stored_token_hash is not None and target in _TERMINAL:
                    supplied_hash = (hashlib.sha256(lease_token.encode("utf-8")).hexdigest()
                                     if isinstance(lease_token, str) else None)
                    if (supplied_hash != stored_token_hash or row["lease_expires_at"] is None
                            or row["lease_expires_at"] <= now):
                        raise RunError("Worker lease is invalid or expired")
                elif stored_token_hash is None and lease_token is not None:
                    raise RunError("Worker lease is no longer active")
                if current == RunStatus.CANCELLING and target == RunStatus.COMPLETED:
                    target, detail, result_json, clean_error = (
                        RunStatus.CANCELLED, "Run cancelled", None, None
                    )
                if target not in _ALLOWED_TRANSITIONS.get(current, set()):
                    raise RunError(f"Invalid lifecycle transition: {current.value} -> {target.value}")
                terminal = target in _TERMINAL
                connection.execute(
                    "UPDATE runs SET status=?, updated_at=?, result_json=?, error=?, "
                    "lease_token_hash=?, lease_expires_at=? WHERE tenant_id=? AND run_id=?",
                    (target.value, now, result_json if target == RunStatus.COMPLETED else None,
                     clean_error, None if terminal else stored_token_hash,
                     None if terminal else row["lease_expires_at"], tenant_id, run_id),
                )
                connection.execute(
                    "INSERT INTO run_events (run_id, status, timestamp, detail) VALUES (?, ?, ?, ?)",
                    (run_id, target.value, now, detail[:200]),
                )
                if terminal:
                    self._settle_usage(connection, run_id, usage, row["attempt_count"])
                snapshot = self._snapshot(connection, tenant_id, run_id)
                connection.execute("COMMIT")
                return snapshot
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    @staticmethod
    def _snapshot(
        connection: sqlite3.Connection, tenant_id: str, run_id: str
    ) -> RunSnapshot | None:
        row = connection.execute(
            "SELECT run_id, status, created_at, updated_at, result_json, error "
            "FROM runs WHERE tenant_id=? AND run_id=?", (tenant_id, run_id),
        ).fetchone()
        if row is None:
            return None
        events = connection.execute(
            "SELECT status, timestamp, detail FROM run_events WHERE run_id=? ORDER BY event_id",
            (run_id,),
        ).fetchall()
        return RunSnapshot(
            run_id=row["run_id"],
            status=RunStatus(row["status"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            result=json.loads(row["result_json"]) if row["result_json"] is not None else None,
            error=row["error"],
            events=tuple(RunEvent(RunStatus(event["status"]), event["timestamp"], event["detail"])
                        for event in events),
        )
