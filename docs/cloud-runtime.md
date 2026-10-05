# Cloud runner foundation (developer preview)

`niji.cloud_runtime` defines a provider-neutral run API, durable stores, and a PostgreSQL-backed worker. The Render Blueprint describes an API, worker, and private Postgres database, but it has **not** been deployed or independently verified against a live Render account.

Worker execution defaults to safe **prompt-only** mode. An explicit `NIJI_CLOUD_EXECUTION_MODE=sandbox` enables the optional E2B coding mode: model calls and provider credentials stay in the trusted worker; a small file/shell tool allowlist is bridged to a fresh E2B sandbox with outbound internet disabled. Sandbox requests may include at most 100 plain-text project files (64 KB each, 500 KB total), a Base64 ZIP archive, or a public GitHub repository imported from a pinned 40-character commit SHA. Both archives and repository source are capped at 500 KB compressed and 500 KB extracted; paths, symlinks, binary files, and likely credentials are rejected before any files are copied. Repository import accepts only exact HTTPS `github.com` URLs, downloads from the fixed `codeload.github.com` host without redirects or proxy environment settings, and rejects private repositories. Other Git hosts, branch names, and private-repository credentials are not supported. Successful sandbox runs can return bounded generated artifacts through private, authenticated download routes. Artifacts are stored in Postgres and cleaned up hourly after the configured retention window (default 7 days). Live E2B verification remains pending. This is an early coding preview, not a production security certification.

## Included

1. `Runner` contract: submit, inspect, and cancel runs without coupling callers to a cloud vendor.
2. Unique private workspace directory per accepted local run; explicit purge for completed runs.
3. Validated lifecycle transitions with a timestamped event trail.
4. Cooperative cancellation and deadlines. Worker callbacks should call `context.check_cancelled()` at safe points; Python cannot forcibly stop an arbitrary handler thread.
5. Idempotency-key deduplication: equivalent retries return the existing run; reusing a key for changed input is rejected.
6. `SQLiteRunStore`: local/single-host durable run state and transactional idempotency; `PostgresRunStore`: multi-process durable run state and cross-worker coordination for hosted deployments.
7. Atomic worker claims use short-lived random lease tokens (stored only as hashes), lease renewal, expired-lease recovery, bounded attempts, and protection against stale workers publishing results. Workers retain a live lease while an in-flight provider operation unwinds after cancellation/deadline when the store remains reachable; ownership loss cancels execution. PostgreSQL uses `FOR UPDATE SKIP LOCKED` for concurrent workers.
8. Optional `cloud_api`: bearer-authenticated endpoints for submitting prompt-only runs, listing and inspecting run state, and requesting cancellation. The tenant key is configured by the server, not accepted from a request body; API request bodies are bounded.
9. `cloud_worker`: polls Postgres, claims and renews leases, observes API cancellation and request deadlines, and stores bounded results; oversized text replies are truncated before persistence instead of leaving runs stuck for lease retries. It publishes only fixed activity labels—not prompts, generated reasoning, tool arguments, filesystem paths, command output, or provider errors—to the event trail.
10. Optional `cloud_sandbox`: creates one E2B sandbox per coding run with outbound network disabled, no worker secrets forwarded, and an allowlist (`bash`, `read_file`, `write_file`, `edit_file`, `list_files`). `bash` intentionally permits arbitrary commands inside the isolated VM, including package installation and child-process creation; it is not a restricted command language. The command bridge caps individual call duration/output, and the VM is killed on completion or failure, but actual CPU, memory, disk, process, cancellation, and provider-account spend enforcement must be verified against the live E2B account before enabling sandbox mode. The SDK is pinned; standard CI uses mocked provider tests, not a live E2B account.
11. Bounded sandbox artifacts are written under the active worker lease, stored in Postgres, and listed/downloaded only after successful completion. Limits are 50 artifacts and 5 MB per run, with 1 MB maximum per file; secret-like/generated paths are denied. Hourly cleanup honors `NIJI_CLOUD_ARTIFACT_RETENTION_DAYS` (1–90 days, default 7).
12. The public `/healthz` readiness check runs a lightweight database query; it returns `503` without exposing connection diagnostics when storage is unavailable. PostgreSQL bootstrap and migrations are serialized by a transaction-scoped advisory lock; the schema version prevents repeating application-table migrations on current databases. Each migration validates the expected prior schema before advancing.
13. Authenticated run admission enforces durable per-user caps on active queued/running work and fixed-hour submissions in SQLite/PostgreSQL. Defaults are 5 active and 20 accepted runs/hour (bounded settings); duplicate idempotent retries do not consume quota, and `429` responses include `Retry-After`.
14. Authenticated HTTP routes also have persistent per-user and per-client-IP minute buckets, shared across API processes through SQLite/PostgreSQL. Defaults are 120 requests/minute per identity and per IP; counters are atomic, bounded, expire with the window, and are stored under SHA-256 bucket IDs rather than raw IPs. Unauthenticated requests consume the IP bucket to make credential guessing more expensive. Forwarded IP headers are ignored by default; only configure `NIJI_CLOUD_TRUSTED_PROXY_CIDRS` for verified proxy ranges, because trusting arbitrary `X-Forwarded-For` values would let clients evade the IP limit.
15. Durable monthly prompt/completion quotas reserve each run's configured maximum before acceptance and atomically settle on completion. Defaults are 2,000,000 prompt and 160,000 completion tokens/month, with per-run caps of 200,000 and 16,384 tokens. Set `NIJI_CLOUD_MAX_MONTHLY_PROMPT_TOKENS`, `NIJI_CLOUD_MAX_MONTHLY_COMPLETION_TOKENS`, `NIJI_CLOUD_RUN_PROMPT_TOKEN_CAP`, and `NIJI_CLOUD_RUN_COMPLETION_TOKEN_CAP` consistently on both API and worker. A single final usage record with valid non-negative integer counts is required; missing, partial, ambiguous, or over-reservation provider usage is charged at the full reserved ceiling. Cancellation before execution releases the reservation.
16. An optional monthly model-spend circuit breaker is enabled only when all three settings are present: `NIJI_CLOUD_MAX_MONTHLY_SPEND_USD`, `NIJI_CLOUD_INPUT_PRICE_USD_PER_MILLION_TOKENS`, and `NIJI_CLOUD_OUTPUT_PRICE_USD_PER_MILLION_TOKENS`. Prices are operator-supplied estimates, rounded upward to micro-USD. Configure the same price values on API and worker so reservations and settlements agree. This cap covers model-token charges only; it does **not** meter E2B sandbox compute or guarantee the provider's invoice total.

## Using durable run records

```python
from niji.cloud_runtime import RunRequest, RunStatus
from niji.durable_run_store import SQLiteRunStore

store = SQLiteRunStore("/var/lib/niji/runs.sqlite3")
snapshot, created = store.create(
    tenant_id="account-123",  # derive from verified authentication, never client input
    request=RunRequest("request-unique-key", {"goal": "summarize"}),
)
```

For a hosted multi-service deployment, configure `NIJI_CLOUD_DATABASE_URL` and use `PostgresRunStore`:

```python
from niji.cloud_runtime import RunRequest
from niji.postgres_run_store import PostgresRunStore

store = PostgresRunStore(os.environ["NIJI_CLOUD_DATABASE_URL"])
snapshot, created = store.create(
    "account-123", RunRequest("request-unique-key", {"goal": "summarize"})
)
```

The Render Blueprint creates a private Postgres database and supplies its internal connection string to the API service. Credentials must remain in the service environment; never pass the database URL to an agent prompt or sandbox.

## Worker leases and retries

A trusted worker can claim one queued run at a time. Claims use a random lease token (only its digest is persisted); workers must renew active leases and present the live token to finish. Expired work can be reclaimed up to a bounded attempt count, and stale workers cannot overwrite newer attempts. SQLite provides this for one host; PostgreSQL coordinates multiple hosts.

```python
claim = store.claim_next("account-123", lease_seconds=60, max_attempts=3)
if claim is not None:
    # Execute only in a separately isolated worker; this store does not sandbox code.
    if not store.renew_lease("account-123", claim.run_id, claim.lease_token):
        raise RuntimeError("Lease lost")
    store.finish_claim(
        "account-123", claim.run_id, claim.lease_token,
        RunStatus.COMPLETED, "Run completed", result={"summary": "done"},
    )
```

The SQLite implementation relies on local locking and is only for a single host. The PostgreSQL implementation uses database row locks to coordinate multiple Render API/worker processes. Neither adapter runs the agent by itself.

## Authenticated run API

Install the optional service dependencies and start the API with:

```sh
pip install 'niji-agent[cloud]'
export NIJI_CLOUD_API_TOKEN='replace-with-a-random-secret-at-least-32-characters-long'
export NIJI_CLOUD_TENANT_ID='your-account-id'
export NIJI_CLOUD_DATABASE='/var/data/niji-runs.sqlite3'
python -m niji.cloud_api
```

The API provides `GET /healthz`, authenticated `GET /v1/capabilities`, `POST /v1/runs`, `GET /v1/runs`, `GET /v1/runs/{run_id}`, `POST /v1/runs/{run_id}/cancel`, `GET /v1/runs/{run_id}/artifacts`, `GET /v1/runs/{run_id}/artifacts/{artifact_id}`, and `DELETE /v1/account/data`. The capabilities route reports the effective API execution mode and supported project/artifact/deletion features without exposing credentials; keep the API and worker execution-mode variables identical. Artifact files are private attachments and available only after the run completes. All authenticated routes require `Authorization: Bearer <token>`. The data-deletion route also requires `X-Confirm-Data-Deletion: delete`; it permanently erases that verified user's stored runs, events, artifacts, usage/quota rows, and per-user request-limit bucket, but refuses with `409` while any run is queued/running/cancelling. Cancel or wait for active runs, then retry. This does not delete the identity-provider account, shared IP throttles, or already-created managed database backups; backup expiry and any legal-retention policy must be configured separately. In `NIJI_CLOUD_AUTH_MODE=oidc`, the bearer must be a signed RS256 access token with valid `iss`, `aud`, `exp`, `iat`, and `sub` claims; signing keys are fetched from the fixed HTTPS `NIJI_CLOUD_OIDC_JWKS_URL`, and the issuer/subject pair maps to an opaque user partition. The Render Blueprint selects this mode and requires trusted values for `NIJI_CLOUD_OIDC_ISSUER`, `NIJI_CLOUD_OIDC_AUDIENCE`, and `NIJI_CLOUD_OIDC_JWKS_URL`. The legacy `bearer` mode uses one configured server tenant and is for single-owner preview/testing only. API docs and OpenAPI endpoints are disabled. Run creation enforces `NIJI_CLOUD_MAX_ACTIVE_RUNS` (default 5, max 100) and `NIJI_CLOUD_MAX_SUBMISSIONS_PER_HOUR` (default 20, max 1,000) per authenticated user; rejected work receives `429` with `Retry-After`. Prompt-only submission payloads contain one non-empty `prompt` field, up to 100 KB. Sandbox mode additionally permits either a validated `files` list or `archive_base64` field containing a ZIP archive. Archives are capped at 500 KB compressed and extracted, 100 files, and 64 KB per file; only UTF-8 text is accepted. Path traversal, symlinks, encrypted/unsupported ZIP entries, duplicate or likely credential paths, binary files, and oversized entries are rejected.

Configure endpoint request throttling with `NIJI_CLOUD_MAX_REQUESTS_PER_MINUTE` and `NIJI_CLOUD_MAX_REQUESTS_PER_IP_PER_MINUTE` (both default to 120, max 10,000, fixed 60-second windows). Clients over the limit receive `429` with `Retry-After`. By default the API uses the direct network peer address and ignores forwarded-IP headers. If a trusted load balancer/proxy is in front, set `NIJI_CLOUD_TRUSTED_PROXY_CIDRS` to its verified CIDR ranges; the API then walks the forwarded chain from the trusted peer inward and chooses the first untrusted address. An omitted/invalid proxy range never causes a forwarded header to be trusted.

Configure the worker's private model credentials with `NIJI_CLOUD_PROVIDER` (defaults to `openai`), `NIJI_CLOUD_PROVIDER_API_KEY`, and optional `NIJI_CLOUD_BASE_URL` / `NIJI_CLOUD_MODEL`. The NVIDIA preset uses `https://integrate.api.nvidia.com/v1` with `nvidia/nemotron-3.5-lightning-30b-a3b`; for that model Niji passes NVIDIA's documented `chat_template_kwargs.enable_thinking=false` so short replies do not spend their output budget on reasoning. The model API key is configured as a private worker secret; it is not accepted in requests or copied into a sandbox. To let the signed-in Home model picker show every model available to that NVIDIA account, the Railway API service also needs `NIJI_CLOUD_PROVIDER_API_KEY` set to the Railway service-variable reference `${{niji-cloud-worker.NIJI_CLOUD_PROVIDER_API_KEY}}` (do not copy the key into Vercel). The API uses this secret only to make a cached HTTPS `GET /v1/models` request; the secret and endpoint are never returned to the browser. If model discovery is not configured or temporarily unavailable, the API retains the two previously supported Nemotron choices. `NIJI_CLOUD_MAX_TURNS`, `NIJI_CLOUD_MAX_ATTEMPTS`, `NIJI_CLOUD_LEASE_SECONDS`, and `NIJI_CLOUD_POLL_SECONDS` are bounded worker settings. `NIJI_CLOUD_WORKER_TENANT_MODE=single` (default) limits a worker to `NIJI_CLOUD_TENANT_ID`; `all` lets a shared worker atomically lease runs across authenticated users, returning the run's owner partition with each claim and using it for heartbeats, artifacts, progress, and finalization.

Prompt-only mode remains the default. To opt into sandbox coding mode, configure `NIJI_CLOUD_EXECUTION_MODE=sandbox` and `E2B_API_KEY` as a private worker secret. The key is read only by the trusted SDK process; no environment variables are supplied to the sandbox. Sandbox runs are capped at 3,600 seconds, have no internet egress, and may receive bounded plain-text project files, ZIP archives, or a public GitHub source archive pinned to a full commit SHA. Repository downloading happens in the trusted worker from GitHub's fixed archive host, with strict size/path/text validation, before any files enter the offline sandbox. The exposed `bash` tool permits arbitrary shell commands inside E2B (including package installation and child-process creation); it is not disabled or restricted to a command allowlist. File operations are confined to the sandbox workspace, and shell/file outputs are bounded. Never accept sandbox network settings or secrets from run payloads. Keep sandbox mode disabled until live E2B resource, process, network, cancellation, teardown, and cost controls have been verified.

The Render Blueprint describes a private managed Postgres database, OIDC-authenticated API service, and multi-tenant background worker sharing the same database. It is still only a deployment recipe; this agent has not created or deployed any Render resource. The OIDC implementation supports a fixed issuer/JWKS and RS256, but the real provider's issuer, audience, and JWKS URL still need account-specific configuration and live verification. The E2B adapter is optional and not live-verified. Public GitHub source import is supported only when pinned to a full commit SHA; private-repository imports are intentionally unsupported. Before a production launch, live OIDC and E2B verification, end-to-end testing, production security review, backup/restore verification, data-retention and deletion decisions, and secret rotation procedures remain. Durable per-user/IP request limits and monthly token quotas are implemented. The optional USD circuit breaker requires operator-supplied model prices, and there is no sandbox-compute spend cap. Bounded text-only initial project files and generated artifacts are supported only in sandbox mode. Artifacts currently use Postgres BYTEA; for larger production workloads, evaluate private object storage rather than growing the database. Review current Render and E2B pricing before creating resources. See Render's [Blueprint spec](https://render.com/docs/blueprint-spec), [background worker docs](https://render.com/docs/background-workers), and [Postgres docs](https://render.com/docs/postgresql).

`SQLiteRunStore` is the local/single-host adapter; `PostgresRunStore` provides durable hosted state and multi-worker claim coordination. Neither is an authentication system—the tenant identifier is a storage partition key, and the trusted API must derive it from verified identity. Do not put provider API keys, database credentials, or other secrets in run payloads.

## Still required before a production hosted service

Use [`production-operations.md`](production-operations.md) for the staged backup/restore, secret-rotation, identity, proxy, and live smoke-test checklist. The checklist is operational guidance only; none of these live account checks has been performed.

- Configure the OIDC issuer, audience, and JWKS URL for the chosen identity provider; verify its access-token format against the live provider before opening hosted sign-in.
- After the API is reachable, verify `request.client.host` and the trusted proxy chain as seen by the app; configure `NIJI_CLOUD_TRUSTED_PROXY_CIDRS` only with confirmed Render proxy ranges. Until then, forwarded IP headers remain deliberately ignored and shared-edge IP throttling may affect unrelated clients.
- Private-repository support remains intentionally unavailable; imports are public GitHub-only and require a pinned full commit SHA.
- Live E2B sandbox integration verification; confirm account/template CPU, memory, disk, process quotas, and test network denial/cancellation against the real provider.
- The optional USD circuit breaker estimates configured model-token charges only; it is not an E2B compute-spend meter or invoice guarantee. Keep coding/sandbox mode disabled until the provider account and compute limits are reviewed.
- End-to-end tests for cancellation during live remote operations, lease loss, network denial, and artifact handling.
- Production security review, durable audit-log/retention policy, safe secret rotation, and a documented tenant-data/backup deletion policy. An authenticated hard-delete route for stored run data is available, but it cannot erase managed-database backup snapshots or the identity-provider account.
- Configure managed database backups and complete a restore drill; the Blueprint does not itself prove backup coverage or recovery objectives.
- Render deployment and health verification after the user connects the account; the Blueprint alone is not deployment.

SQLite database files are created with private file permissions where supported. These controls help protect a single-host development deployment; they do not substitute for host hardening, encryption, authentication, or cloud-provider security review. Firebase credentials were not available for provider verification.
