"""Authenticated HTTP control plane for the durable cloud run store.

This API accepts and manages run records. A worker service must consume queued
runs before requests can execute; API acceptance alone never starts agent work.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import re
from urllib.parse import quote
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, ValidationError
from starlette.concurrency import run_in_threadpool

from .cloud_auth import InvalidIdentityToken, OIDCVerifier
from .cloud_usage import (DEFAULT_MONTHLY_COMPLETION_TOKENS, DEFAULT_MONTHLY_PROMPT_TOKENS,
                          DEFAULT_RUN_COMPLETION_TOKENS, DEFAULT_RUN_PROMPT_TOKENS,
                          cost_micros, parse_price_micros_per_million, parse_usd_micros)
from .cloud_runtime import (ActiveRunsPreventDeletion, IdempotencyConflict, RunError,
                            RunLimitExceeded, RunRequest, RunSnapshot, RunStatus,
                            validate_cloud_payload)
from .cloud_models import public_model_catalog
from .durable_run_store import SQLiteRunStore
from .postgres_run_store import PostgresRunStore

_MAX_BODY_BYTES = 1_050_000
_TENANT_ID = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")


def _bounded_limit(name: str, value: int | None, default: int, maximum: int) -> int:
    raw: Any = value if value is not None else os.environ.get(name, str(default))
    if isinstance(raw, bool):
        raise ValueError(f"{name} must be an integer between 1 and {maximum}")
    try:
        parsed = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer between 1 and {maximum}") from exc
    if str(parsed) != str(raw).strip() or not 1 <= parsed <= maximum:
        raise ValueError(f"{name} must be an integer between 1 and {maximum}")
    return parsed


def _parse_trusted_proxy_cidrs(value: str | None) -> tuple[Any, ...]:
    if value is None:
        return ()
    if not isinstance(value, str) or len(value) > 4096:
        raise ValueError("NIJI_CLOUD_TRUSTED_PROXY_CIDRS is invalid")
    if not value.strip():
        return ()
    try:
        return tuple(
            ipaddress.ip_network(item.strip(), strict=False)
            for item in value.split(",") if item.strip()
        )
    except ValueError as exc:
        raise ValueError("NIJI_CLOUD_TRUSTED_PROXY_CIDRS is invalid") from exc


def _client_ip(request: Request, trusted_proxies: tuple[Any, ...]) -> str:
    """Resolve a peer address; consult X-Forwarded-For only behind trusted proxies."""
    peer_raw = request.client.host if request.client is not None else ""
    try:
        peer = ipaddress.ip_address(peer_raw)
    except ValueError:
        return "unknown"

    def is_trusted(address: Any) -> bool:
        return any(address.version == network.version and address in network
                   for network in trusted_proxies)

    if not is_trusted(peer):
        return peer.compressed
    forwarded = request.headers.get("x-forwarded-for", "")
    if not forwarded or len(forwarded) > 2048:
        return peer.compressed
    raw_chain = forwarded.split(",")
    if len(raw_chain) > 32:
        return peer.compressed
    try:
        chain = [ipaddress.ip_address(item.strip()) for item in raw_chain]
    except ValueError:
        return peer.compressed
    chain.append(peer)
    for address in reversed(chain):
        if not is_trusted(address):
            return address.compressed
    return peer.compressed


class _SubmitRunBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str
    payload: dict[str, Any]
    timeout_seconds: Any = 900


def create_app(
    *,
    store: Any | None = None,
    api_token: str | None = None,
    tenant_id: str | None = None,
    execution_mode: str | None = None,
    auth_mode: str | None = None,
    oidc_issuer: str | None = None,
    oidc_audience: str | None = None,
    oidc_jwks_url: str | None = None,
    oidc_verifier: OIDCVerifier | None = None,
    max_active_runs: int | None = None,
    max_submissions_per_hour: int | None = None,
    monthly_prompt_token_limit: int | None = None,
    monthly_completion_token_limit: int | None = None,
    run_prompt_token_reservation: int | None = None,
    run_completion_token_reservation: int | None = None,
    max_requests_per_minute: int | None = None,
    max_requests_per_ip_per_minute: int | None = None,
    trusted_proxy_cidrs: str | None = None,
) -> FastAPI:
    """Build the API with either fixed-token preview auth or verified OIDC.

    ``NIJI_CLOUD_AUTH_MODE=oidc`` derives an opaque storage owner from the verified
    issuer/subject. The compatibility bearer mode uses one server-configured tenant
    and should not be exposed as multi-user production authentication.
    """
    mode_auth = auth_mode if auth_mode is not None else os.environ.get("NIJI_CLOUD_AUTH_MODE", "bearer")
    mode_auth = mode_auth.strip().lower() if isinstance(mode_auth, str) else ""
    if mode_auth not in {"bearer", "oidc"}:
        raise ValueError("NIJI_CLOUD_AUTH_MODE must be 'bearer' or 'oidc'")
    token = api_token if api_token is not None else os.environ.get("NIJI_CLOUD_API_TOKEN", "")
    owner = tenant_id if tenant_id is not None else os.environ.get("NIJI_CLOUD_TENANT_ID", "")
    verifier = oidc_verifier
    if mode_auth == "bearer":
        if not isinstance(token, str) or len(token) < 32:
            raise ValueError("NIJI_CLOUD_API_TOKEN must contain at least 32 characters")
        if not isinstance(owner, str) or not _TENANT_ID.fullmatch(owner):
            raise ValueError("NIJI_CLOUD_TENANT_ID must be 1-200 safe characters")
    else:
        issuer = oidc_issuer if oidc_issuer is not None else os.environ.get("NIJI_CLOUD_OIDC_ISSUER", "")
        audience = oidc_audience if oidc_audience is not None else os.environ.get("NIJI_CLOUD_OIDC_AUDIENCE", "")
        jwks_url = oidc_jwks_url if oidc_jwks_url is not None else os.environ.get("NIJI_CLOUD_OIDC_JWKS_URL", "")
        if verifier is None:
            verifier = OIDCVerifier(issuer, audience, jwks_url)
        token = ""
        owner = ""
    database_url = os.environ.get("NIJI_CLOUD_DATABASE_URL", "").strip()
    database = os.environ.get("NIJI_CLOUD_DATABASE", "./var/niji-runs.sqlite3")
    mode = execution_mode if execution_mode is not None else os.environ.get("NIJI_CLOUD_EXECUTION_MODE", "prompt")
    mode = mode.strip().lower() if isinstance(mode, str) else ""
    if mode not in {"prompt", "sandbox"}:
        raise ValueError("NIJI_CLOUD_EXECUTION_MODE must be 'prompt' or 'sandbox'")
    active_limit = _bounded_limit("NIJI_CLOUD_MAX_ACTIVE_RUNS", max_active_runs, 5, 100)
    hourly_limit = _bounded_limit(
        "NIJI_CLOUD_MAX_SUBMISSIONS_PER_HOUR", max_submissions_per_hour, 20, 1000
    )
    user_request_limit = _bounded_limit(
        "NIJI_CLOUD_MAX_REQUESTS_PER_MINUTE", max_requests_per_minute, 120, 10_000
    )
    ip_request_limit = _bounded_limit(
        "NIJI_CLOUD_MAX_REQUESTS_PER_IP_PER_MINUTE",
        max_requests_per_ip_per_minute, 120, 10_000,
    )
    monthly_prompt_limit = _bounded_limit(
        "NIJI_CLOUD_MAX_MONTHLY_PROMPT_TOKENS", monthly_prompt_token_limit,
        DEFAULT_MONTHLY_PROMPT_TOKENS, 10_000_000_000,
    )
    monthly_completion_limit = _bounded_limit(
        "NIJI_CLOUD_MAX_MONTHLY_COMPLETION_TOKENS", monthly_completion_token_limit,
        DEFAULT_MONTHLY_COMPLETION_TOKENS, 10_000_000_000,
    )
    run_prompt_reservation = _bounded_limit(
        "NIJI_CLOUD_RUN_PROMPT_TOKEN_CAP", run_prompt_token_reservation,
        DEFAULT_RUN_PROMPT_TOKENS, 10_000_000_000,
    )
    run_completion_reservation = _bounded_limit(
        "NIJI_CLOUD_RUN_COMPLETION_TOKEN_CAP", run_completion_token_reservation,
        DEFAULT_RUN_COMPLETION_TOKENS, 10_000_000_000,
    )
    if run_prompt_reservation > monthly_prompt_limit or run_completion_reservation > monthly_completion_limit:
        raise ValueError("Per-run token caps cannot exceed the monthly token budgets")
    spend_raw = os.environ.get("NIJI_CLOUD_MAX_MONTHLY_SPEND_USD", "").strip()
    input_price_raw = os.environ.get("NIJI_CLOUD_INPUT_PRICE_USD_PER_MILLION_TOKENS", "").strip()
    output_price_raw = os.environ.get("NIJI_CLOUD_OUTPUT_PRICE_USD_PER_MILLION_TOKENS", "").strip()
    pricing_values = (spend_raw, input_price_raw, output_price_raw)
    if any(pricing_values) and not all(pricing_values):
        raise ValueError("Monthly USD spend budget requires its limit and both explicit token prices")
    if all(pricing_values):
        monthly_cost_limit = parse_usd_micros(spend_raw, "NIJI_CLOUD_MAX_MONTHLY_SPEND_USD")
        input_price = parse_price_micros_per_million(
            input_price_raw, "NIJI_CLOUD_INPUT_PRICE_USD_PER_MILLION_TOKENS")
        output_price = parse_price_micros_per_million(
            output_price_raw, "NIJI_CLOUD_OUTPUT_PRICE_USD_PER_MILLION_TOKENS")
        run_cost_reservation = cost_micros(
            run_prompt_reservation, run_completion_reservation, input_price, output_price,
        )
        if run_cost_reservation > monthly_cost_limit:
            raise ValueError("Per-run token caps exceed the configured monthly spend budget")
    else:
        monthly_cost_limit = None
        input_price = output_price = run_cost_reservation = 0
    proxy_setting = (trusted_proxy_cidrs if trusted_proxy_cidrs is not None
                     else os.environ.get("NIJI_CLOUD_TRUSTED_PROXY_CIDRS", ""))
    trusted_proxies = _parse_trusted_proxy_cidrs(proxy_setting)
    if store is not None:
        run_store = store
    elif database_url:
        run_store = PostgresRunStore(database_url)
    else:
        run_store = SQLiteRunStore(database)

    app = FastAPI(
        title="Niji Cloud Runs API",
        version="1",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.run_store = run_store
    app.state.api_token = token
    app.state.tenant_id = owner
    app.state.auth_mode = mode_auth
    app.state.oidc_verifier = verifier
    app.state.execution_mode = mode
    app.state.max_active_runs = active_limit
    app.state.max_submissions_per_hour = hourly_limit
    app.state.monthly_prompt_token_limit = monthly_prompt_limit
    app.state.monthly_completion_token_limit = monthly_completion_limit
    app.state.run_prompt_token_reservation = run_prompt_reservation
    app.state.run_completion_token_reservation = run_completion_reservation
    app.state.monthly_cost_limit_micros = monthly_cost_limit
    app.state.input_price_micros_per_million = input_price
    app.state.output_price_micros_per_million = output_price
    app.state.run_cost_reservation_micros = run_cost_reservation
    app.state.max_requests_per_minute = user_request_limit
    app.state.max_requests_per_ip_per_minute = ip_request_limit
    app.state.trusted_proxy_cidrs = trusted_proxies

    async def consume_request_bucket(key: str, limit: int) -> None:
        consume = getattr(app.state.run_store, "consume_rate_limit", None)
        if not callable(consume):
            raise HTTPException(status_code=503, detail="Request admission controls are unavailable")
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        try:
            retry_after = await run_in_threadpool(
                consume, digest, limit=limit, window_seconds=60
            )
        except Exception as exc:
            raise HTTPException(status_code=503, detail="Request admission controls are unavailable") from exc
        if retry_after is not None:
            raise HTTPException(
                status_code=429,
                detail="Request rate limit exceeded",
                headers={"Retry-After": str(retry_after)},
            )

    async def authenticate(request: Request) -> None:
        ip = _client_ip(request, app.state.trusted_proxy_cidrs)
        await consume_request_bucket("ip:" + ip, app.state.max_requests_per_ip_per_minute)
        header = request.headers.get("authorization", "")
        scheme, separator, credential = header.partition(" ")
        invalid = scheme.lower() != "bearer" or not separator or not credential
        tenant = None
        if not invalid and app.state.auth_mode == "bearer":
            invalid = not hmac.compare_digest(
                credential.encode("utf-8"), app.state.api_token.encode("utf-8")
            )
            tenant = app.state.tenant_id
        elif not invalid:
            if len(credential) > 16_384:
                invalid = True
            else:
                try:
                    tenant = await run_in_threadpool(
                        app.state.oidc_verifier.tenant_for_token, credential
                    )
                except InvalidIdentityToken:
                    invalid = True
        if invalid or tenant is None:
            raise HTTPException(
                status_code=401,
                detail="Valid bearer token required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        request.state.niji_tenant_id = tenant
        await consume_request_bucket("tenant:" + tenant, app.state.max_requests_per_minute)

    async def read_bounded_json(request: Request) -> bytes:
        declared_length = request.headers.get("content-length")
        if declared_length:
            try:
                if int(declared_length) > _MAX_BODY_BYTES:
                    raise HTTPException(status_code=413, detail="Request body is too large")
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="Invalid Content-Length") from exc
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > _MAX_BODY_BYTES:
                raise HTTPException(status_code=413, detail="Request body is too large")
            body.extend(chunk)
        return bytes(body)

    @app.get("/healthz")
    def health() -> dict[str, str]:
        """Readiness probe: report ready only when the run database is reachable."""
        check = getattr(app.state.run_store, "healthcheck", None)
        if callable(check):
            try:
                if not check():
                    raise HTTPException(status_code=503, detail="Service not ready")
            except HTTPException:
                raise
            except Exception as exc:
                raise HTTPException(status_code=503, detail="Service not ready") from exc
        return {"status": "ok"}

    @app.delete("/v1/account/data")
    async def delete_account_data(request: Request) -> dict[str, Any]:
        """Delete the authenticated tenant's stored run data after explicit confirmation."""
        await authenticate(request)
        if request.headers.get("x-confirm-data-deletion", "").strip().lower() != "delete":
            raise HTTPException(
                status_code=400,
                detail="Set X-Confirm-Data-Deletion: delete to confirm permanent data removal",
            )
        delete_data = getattr(app.state.run_store, "delete_tenant_data", None)
        if not callable(delete_data):
            raise HTTPException(status_code=503, detail="Data deletion is unavailable")
        try:
            deleted_runs = await run_in_threadpool(
                delete_data, request.state.niji_tenant_id
            )
        except ActiveRunsPreventDeletion as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=503, detail="Data deletion is unavailable") from exc
        return {"deleted_runs": deleted_runs, "deleted": True}

    @app.get("/v1/capabilities")
    async def get_capabilities(request: Request) -> dict[str, Any]:
        """Report the effective cloud feature set for the signed-in user."""
        await authenticate(request)
        sandbox_enabled = app.state.execution_mode == "sandbox"
        run_store = app.state.run_store
        models = await run_in_threadpool(public_model_catalog) if app.state.execution_mode == "prompt" else []
        return {
            "product": "Niji Agent",
            "execution_mode": app.state.execution_mode,
            "models": models,
            "features": {
                "prompt_runs": True,
                "isolated_project_workspace": sandbox_enabled,
                "text_file_upload": sandbox_enabled,
                "public_pinned_github_import": sandbox_enabled,
                "artifact_download": callable(getattr(run_store, "list_artifacts", None))
                and callable(getattr(run_store, "get_artifact", None)),
                "account_data_deletion": callable(getattr(run_store, "delete_tenant_data", None)),
            },
            "sandbox_tools": ["bash", "read_file", "write_file", "edit_file", "list_files"]
            if sandbox_enabled else [],
        }

    @app.post("/v1/runs", status_code=202)
    async def submit_run(request: Request, response: Response) -> dict[str, Any]:
        await authenticate(request)
        raw = await read_bounded_json(request)
        try:
            body = _SubmitRunBody.model_validate_json(raw)
            await run_in_threadpool(validate_cloud_payload, body.payload)
            if "model" in body.payload and app.state.execution_mode != "prompt":
                raise ValueError("Model selection is available for prompt runs only")
            if ({"files", "archive_base64", "repository"} & set(body.payload)
                    and app.state.execution_mode != "sandbox"):
                raise ValueError("Project files require sandbox execution mode")
            run_request = RunRequest(
                body.idempotency_key, body.payload, body.timeout_seconds
            )
        except (ValidationError, ValueError, TypeError) as exc:
            raise HTTPException(status_code=422, detail="Invalid run request") from exc
        create_limited = getattr(app.state.run_store, "create_limited", None)
        if not callable(create_limited):
            raise HTTPException(status_code=503, detail="Run admission controls are unavailable")
        try:
            snapshot, created = await run_in_threadpool(
                create_limited,
                request.state.niji_tenant_id,
                run_request,
                max_active_runs=app.state.max_active_runs,
                max_submissions_per_hour=app.state.max_submissions_per_hour,
                monthly_prompt_token_limit=app.state.monthly_prompt_token_limit,
                monthly_completion_token_limit=app.state.monthly_completion_token_limit,
                reserved_prompt_tokens=app.state.run_prompt_token_reservation,
                reserved_completion_tokens=app.state.run_completion_token_reservation,
                monthly_cost_limit_micros=app.state.monthly_cost_limit_micros,
                reserved_cost_micros=app.state.run_cost_reservation_micros,
            )
        except RunLimitExceeded as exc:
            raise HTTPException(
                status_code=429,
                detail=str(exc),
                headers={"Retry-After": str(exc.retry_after)},
            ) from exc
        except IdempotencyConflict as exc:
            raise HTTPException(status_code=409, detail="Idempotency key conflicts with an earlier request") from exc
        response.status_code = 202 if created else 200
        response.headers["Location"] = f"/v1/runs/{snapshot.run_id}"
        return _snapshot_json(snapshot)

    @app.get("/v1/runs")
    async def list_runs(
        request: Request,
        limit: int = Query(default=20, ge=1, le=100),
    ) -> dict[str, Any]:
        await authenticate(request)
        snapshots = await run_in_threadpool(
            app.state.run_store.list_recent, request.state.niji_tenant_id, limit=limit
        )
        return {"runs": [_snapshot_json(snapshot) for snapshot in snapshots]}

    @app.get("/v1/runs/{run_id}")
    async def get_run(run_id: str, request: Request) -> dict[str, Any]:
        await authenticate(request)
        try:
            snapshot = await run_in_threadpool(
                app.state.run_store.get, request.state.niji_tenant_id, run_id
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="Run not found") from exc
        if snapshot is None:
            raise HTTPException(status_code=404, detail="Run not found")
        return _snapshot_json(snapshot)

    @app.post("/v1/runs/{run_id}/cancel")
    async def cancel_run(run_id: str, request: Request) -> dict[str, Any]:
        await authenticate(request)
        try:
            snapshot = await run_in_threadpool(
                app.state.run_store.get, request.state.niji_tenant_id, run_id
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="Run not found") from exc
        if snapshot is None:
            raise HTTPException(status_code=404, detail="Run not found")
        if snapshot.status in {
            RunStatus.COMPLETED, RunStatus.CANCELLED, RunStatus.TIMED_OUT,
            RunStatus.FAILED, RunStatus.CANCELLING,
        }:
            return _snapshot_json(snapshot)
        target = RunStatus.CANCELLED if snapshot.status == RunStatus.QUEUED else RunStatus.CANCELLING
        try:
            snapshot = await run_in_threadpool(
                app.state.run_store.transition,
                request.state.niji_tenant_id, run_id, target,
                "Cancelled before execution" if target == RunStatus.CANCELLED else "Cancellation requested",
            )
        except (KeyError, RunError):
            # A worker may have changed state between the lookup and this request.
            snapshot = await run_in_threadpool(
                app.state.run_store.get, request.state.niji_tenant_id, run_id
            )
            if snapshot is None:
                raise HTTPException(status_code=404, detail="Run not found")
        return _snapshot_json(snapshot)

    @app.get("/v1/runs/{run_id}/artifacts")
    async def list_run_artifacts(run_id: str, request: Request) -> dict[str, Any]:
        await authenticate(request)
        try:
            snapshot = await run_in_threadpool(
                app.state.run_store.get, request.state.niji_tenant_id, run_id
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="Run not found") from exc
        if snapshot is None:
            raise HTTPException(status_code=404, detail="Run not found")
        if snapshot.status != RunStatus.COMPLETED:
            raise HTTPException(status_code=409, detail="Artifacts are available after a run completes")
        list_artifacts = getattr(app.state.run_store, "list_artifacts", None)
        if not callable(list_artifacts):
            raise HTTPException(status_code=503, detail="Artifact storage is not configured")
        items = await run_in_threadpool(
            list_artifacts, request.state.niji_tenant_id, run_id
        )
        return {"artifacts": [
            {
                "artifact_id": item.artifact_id,
                "path": item.path,
                "content_type": item.content_type,
                "sha256": item.sha256,
                "size_bytes": item.size_bytes,
                "created_at": item.created_at,
                "download_url": f"/v1/runs/{run_id}/artifacts/{item.artifact_id}",
            }
            for item in items
        ]}

    @app.get("/v1/runs/{run_id}/artifacts/{artifact_id}")
    async def download_run_artifact(run_id: str, artifact_id: str, request: Request) -> Response:
        await authenticate(request)
        try:
            snapshot = await run_in_threadpool(
                app.state.run_store.get, request.state.niji_tenant_id, run_id
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="Run not found") from exc
        if snapshot is None:
            raise HTTPException(status_code=404, detail="Run not found")
        if snapshot.status != RunStatus.COMPLETED:
            raise HTTPException(status_code=409, detail="Artifacts are available after a run completes")
        get_artifact = getattr(app.state.run_store, "get_artifact", None)
        if not callable(get_artifact):
            raise HTTPException(status_code=503, detail="Artifact storage is not configured")
        item = await run_in_threadpool(
            get_artifact, request.state.niji_tenant_id, run_id, artifact_id
        )
        if item is None or item.data is None:
            raise HTTPException(status_code=404, detail="Artifact not found")
        filename = item.path.rsplit("/", 1)[-1]
        return Response(
            content=item.data,
            media_type=item.content_type,
            headers={
                "Content-Disposition": f"attachment; filename=download; filename*=UTF-8''{quote(filename, safe='')}",
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "private, no-store",
                "ETag": f'"{item.sha256}"',
            },
        )

    return app


def _snapshot_json(snapshot: RunSnapshot) -> dict[str, Any]:
    return {
        "run_id": snapshot.run_id,
        "status": snapshot.status.value,
        "created_at": snapshot.created_at,
        "updated_at": snapshot.updated_at,
        "result": snapshot.result,
        "error": snapshot.error,
        "events": [
            {"status": event.status.value, "timestamp": event.timestamp, "detail": event.detail}
            for event in snapshot.events
        ],
    }


def main() -> None:
    """Run the API with Uvicorn (requires the optional cloud extra)."""
    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover - exercised by packaging users
        raise SystemExit("Install Niji's cloud extra: pip install 'niji-agent[cloud]'") from exc
    port = int(os.environ.get("PORT", "10000"))
    uvicorn.run("niji.cloud_api:create_app", host="0.0.0.0", port=port, factory=True)


if __name__ == "__main__":  # pragma: no cover - exercised in service startup
    main()
