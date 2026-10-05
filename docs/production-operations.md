# Hosted operations runbook (pre-launch)

This is an operator checklist for the Niji Cloud API, worker, and PostgreSQL services described in `render.yaml`. It does not create or deploy any service. Keep `NIJI_CLOUD_EXECUTION_MODE=prompt` until live identity, provider, and sandbox checks have passed. Never send passwords, API keys, database URLs, or signing secrets in chat, source control, run payloads, or logs.

## Before enabling sign-in

- Select the identity provider and record its exact access-token issuer, audience, and HTTPS JWKS URL from trusted provider documentation. Configure `NIJI_CLOUD_OIDC_ISSUER`, `NIJI_CLOUD_OIDC_AUDIENCE`, and `NIJI_CLOUD_OIDC_JWKS_URL` only in the API service's private environment. The current verifier accepts RS256 access tokens and requires `iss`, `aud`, `exp`, `iat`, and `sub`.
- Against a staging identity configuration, verify a valid token, an expired token, wrong issuer, wrong audience, missing subject, and a token signed by an untrusted key. Confirm that different issuer/subject pairs cannot read or delete each other's runs.
- `NIJI_CLOUD_TRUSTED_PROXY_CIDRS` is empty by default. After the API is reachable, verify the direct peer address and forwarded chain seen by the application. Only then set this value to the exact trusted proxy CIDRs; never trust arbitrary forwarded headers.
- The fixed-token `bearer` mode is for single-owner preview only. Do not use it as multi-user production sign-in.

## Database backups and restore drill

1. Before launch, confirm the selected managed PostgreSQL plan and account actually provide the backup/PITR coverage and retention you need. Record the expected recovery point and recovery time objectives; the Blueprint does not configure or prove these guarantees.
2. Enable the provider-supported backups in its control panel and restrict access to the database and backup controls. Keep the service connection string private and identical for API and worker.
3. Restore a recent backup into a separate, private, non-production database on a recurring schedule and before major schema changes. Never test restore by overwriting production.
4. Start the current application against the restored database in the isolated environment. Confirm startup migration, `GET /healthz`, one authenticated prompt-only test run, cancellation, and artifact retrieval/cleanup as applicable. Capture pass/fail, backup timestamp, and recovery duration without recording credentials or user prompts.
5. Define who may authorize production recovery, the acceptable data-loss window, and how to pause API submissions and workers during restoration. Keep this procedure and backup access available to a second trusted operator.

**Deletion caveat:** `DELETE /v1/account/data` removes the authenticated user's live run records, events, artifacts, quota/usage rows, and user-specific request bucket. It requires `X-Confirm-Data-Deletion: delete` and returns `409` if any run is queued, running, or cancelling. It does not delete the identity-provider account, shared IP throttles, or copies inside already-created database backups. Set backup retention and document when a deleted record ages out of backups before advertising a deletion window.

## Secret rotation

Rotate through each provider's secure service settings; never paste secret values into chat, a run payload, or a committed file. Record the operator, change window, affected services, verification, and revocation time—not the secret itself.

- **Model-provider API key:** Add the replacement to the worker's `NIJI_CLOUD_PROVIDER_API_KEY`, roll/restart the worker, run a low-cost staging prompt, confirm successful completion and usage accounting, then revoke the old key.
- **OIDC signing keys:** Keep old and new public keys available together during the provider's overlap period. Verify tokens signed by the new key are accepted before retiring the old key. The JWKS client caches key sets for five minutes; allow for that cache and test key rotation rather than assuming it.
- **E2B key:** This is a worker-only secret. Rotate it before any sandbox enablement, verify creation and teardown with a non-sensitive test, and keep sandbox mode off until provider resource, egress, cancellation, and cost limits are checked.
- **Managed database credential:** Use the provider's supported rotation procedure. Update the API and worker consistently, verify both reconnect and pass health/worker checks, then revoke the old credential. Do not put connection strings in command arguments or logs.
- **Preview bearer token:** If single-owner bearer mode is used outside production, generate a random value of at least 32 characters and update the API service securely. Clients must receive the replacement through an approved secret channel. Production should use OIDC instead.

## Pre-launch smoke checklist

- [ ] Private repository access and service credentials are limited to authorized maintainers/operators.
- [ ] OIDC issuer, audience, JWKS, token rejection cases, and per-user isolation verified with the real identity provider.
- [ ] API and worker use the same managed PostgreSQL database; migrations, readiness, backups, and a restore drill verified.
- [ ] Trusted proxy ranges verified from observed connections before configuring IP forwarding trust.
- [ ] Prompt-only real-provider run, usage settlement, cancellation, lease recovery, and error handling verified using a staging key.
- [ ] E2B sandbox remains off until live network denial, resource limits, cancellation, teardown, artifact handling, and spend exposure are verified. The current USD circuit breaker meters configured model-token estimates only; it is not a sandbox-compute cap or invoice guarantee.
- [ ] Data retention, backup expiry after deletion, incident contacts, and secret-rotation owners recorded.
- [ ] User explicitly approves connecting the hosting account and deploying. Do not deploy before that approval.
