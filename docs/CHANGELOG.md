# Changelog (inherited from the original Niji Agent)

## What's new in 2.55.0

- Added an authenticated, explicit-confirmation endpoint to permanently delete the current user's stored runs, events, artifacts, quota/usage records, and user-specific request-limit bucket.
- Deletion is transactionally tenant-scoped, serialized against new run admission, and refuses while queued/running/cancelling work remains; IP-level abuse controls are retained.
- Added API and PostgreSQL integration coverage plus cloud-runtime documentation. The endpoint does not delete identity-provider accounts or managed database backup snapshots; retention and backup policy still require operator decisions.
- The GitHub repository is now private. Anonymous GitHub installation is no longer supported; authorized maintainers should clone with their configured GitHub credentials. The hosted backup/restore, live verification, and secret-rotation checklist is in [`docs/production-operations.md`](docs/production-operations.md).

## What's new in 2.54.1

- Hardened model-usage settlement: ambiguous, incomplete, invalid, or over-reservation provider usage is handled conservatively instead of allowing under/over-accounting.
- Workers retain lease ownership while cancellation or timeout unwinds, so a slow provider response cannot be picked up by a second worker; oversized text results are bounded before durable storage.
- Sandbox file reads now reject symlinks using no-follow file-descriptor traversal, and runtime documentation accurately describes arbitrary shell capabilities and live-verification requirements.
- Validation: 422 local tests pass (19 PostgreSQL integration cases skipped because no local PostgreSQL service is available); clean-source wheel and source distribution builds pass. GitHub CI will validate the published commit against PostgreSQL.
- Live OIDC/E2B/Render checks, production operations/security review, and deployment remain pending. No cloud account has been connected or deployed.

## What's new in 2.54.0

- Added monthly prompt/completion token reservations, per-run caps, and an optional USD spend ceiling using explicitly configured per-million-token prices.
- Settles reported usage atomically; when usage is unavailable it conservatively charges the full reservation, while queued cancellations release unused reservations.
- Corrected spend accounting bounds so valid USD budgets are not rejected by token-count limits; added idempotency, pricing-validation, concurrency, and high-value accounting tests.
- Workers now stop execution when database lease renewal becomes uncertain, preventing possible overlapping runs during outages.
- This estimated USD circuit breaker covers configured model-token prices only—not E2B sandbox compute. Live E2B/OIDC provider verification, production operations/security review, and Render deployment are still pending.

## What's new in 2.53.0

- Added public GitHub repository imports pinned to full 40-character commit SHAs; private repositories and moving branch names are rejected.
- Downloads are limited to GitHub's fixed HTTPS archive host, redirects and ambient proxy settings are disabled, and archives pass the existing bounded path, size, text, and secret checks before entering an offline sandbox.
- Added mocked fetch, SSRF/redirect, private-repository, unsafe archive, cancellation, API, and sandbox integration tests.

## What's new in 2.52.0

- Added persistent per-user and per-client-IP HTTP request throttles shared across API instances using SQLite/PostgreSQL atomic counters; rejected requests receive `429` with `Retry-After`.
- Forwarded client IP headers are ignored unless explicitly configured proxy CIDRs match the direct peer, preventing caller-controlled `X-Forwarded-For` spoofing.
- Added a safe SQLite v5→v6 and PostgreSQL schema upgrade for request counters, plus concurrent, expiry, API, and proxy-trust tests.
- Fixed older SQLite v3/v4 migrations to advance cleanly to the current schema instead of stopping at version 4.

Cloud deployment remains deferred; this release is locally and CI tested, not deployed.

## What's new in 2.51.0

- Added OIDC RS256 access-token verification with fixed issuer, audience, and HTTPS JWKS configuration; verified issuer/subject pairs map to opaque, isolated per-user run partitions.
- The Render Blueprint now fails closed on missing OIDC settings; the worker can lease jobs across users while preserving each run's tenant scope.
- Added user-isolation, invalid-token, and multi-tenant worker/store tests. OIDC provider configuration and live E2B verification still require account-specific setup.

## What's new in 2.50.0

- Sandbox mode accepts bounded `.zip` project uploads as Base64 and validates them fully in memory before the worker copies text files into an isolated workspace.
- Archive uploads reject traversal, symlinks, encrypted/unsupported entries, duplicate or secret-like paths, binary/non-UTF-8 files, oversized entries, and excessive entry counts; limits are 500 KB compressed, 100 files, 64 KB/file, and 500 KB extracted.
- Prompt-only mode continues to reject project uploads; repository URL import and live E2B verification remain pending.

## What's new in 2.49.0

- Added durable per-tenant limits for active queued/running work and hourly run submissions; duplicate idempotent retries do not consume quota.
- API limit responses use `429` with `Retry-After`; Render defaults to 5 active runs and 20 submissions/hour, adjustable within bounded caps.
- Added query indexes and concurrency tests for admission control across multiple API processes.

## What's new in 2.48.0

- PostgreSQL startup and schema migrations are serialized with a transaction-scoped advisory lock, avoiding API/worker startup deadlocks.
- Added schema-shape checks before upgrading older databases and a monotonic migration-version guard.
- Artifact retention now deletes only completed-run files in bounded batches, preserving artifacts belonging to long-running tasks.

## What's new in 2.47.0

- Render's `/healthz` now checks that the run database responds instead of always reporting ready.
- PostgreSQL startup uses a migration version check to avoid repeating table/index creation on current schemas.

## What's new in 2.46.1

- Fixed the artifact cleanup method binding found by the PostgreSQL-backed CI integration tests.

## What's new in 2.46.0

- Sandbox runs can return bounded generated files through tenant/run-scoped artifact list and download endpoints. Files are lease-owned, secret/path filtered, limited to 50 files and 5 MB per run, and only visible after successful completion.
- Added hourly PostgreSQL artifact cleanup with a configurable 1–90 day retention window (default 7 days), plus an additive v1-to-v2 schema migration.
- The worker now receives the configured artifact retention setting; cleanup failures are logged without stopping run processing.
- Artifact storage uses Postgres BYTEA for this preview. Review database size/backup costs before production; object storage is a future scaling option.

## What's new in 2.45.0

- Added sandbox-only initial project file uploads with strict count, per-file/total size, UTF-8 text, path traversal, Windows-path, duplicate-path, generated-folder, and credential-file checks.
- Project files are copied into a fresh network-disabled E2B workspace before the agent starts; prompt-only API mode rejects them. Provider/database credentials remain outside the sandbox.
- Corrected Windows separator and NUL-byte path validation, added API/validator/sandbox upload tests, and fixed the path expectation that caused the previous CI run to fail.
- Still pending: repository URL import, live E2B account verification, and production identity/abuse controls.

## What's new in 2.42.0

- Added a PostgreSQL-backed durable run store with idempotent requests, tenant-scoped reads, atomic `SKIP LOCKED` worker claims, leases, retry recovery, and stale-worker protection.
- Render Blueprint provisions private Postgres so API and worker services can share durable state; live account deployment still requires user setup and approval.

## What's new in 2.41.1

- Fixed live worker-lease cancellation so the API can request cancellation and a late worker completion cannot override it.

## What's new in 2.41.0

- Added atomic worker claims with hashed lease tokens, heartbeat renewal, bounded retries, crash recovery, and stale-worker completion protection.
- Added a safe v1-to-v2 local database migration path; this enables a single-host worker prototype but does not yet execute agent tasks or support multi-host Render scaling.

## What's new in 2.40.0

- Added a Render Blueprint for the authenticated API with an auto-generated bearer secret, health checks, CI-gated deploys, and a persistent disk for SQLite state.
- The blueprint deliberately runs one paid service instance: Render disks are single-instance, and the API still stores queued work without executing it. Check Render's current pricing before creating the service.

## What's new in 2.39.0

- Added an optional authenticated HTTP API for durable run submission, status/history lookup, and cancellation, with a server-configured tenant and bounded request bodies.
- Added a separate `cloud` extra for the API runtime; run submission is persisted, but execution still requires a worker and is not yet hosted by Render.

## What's new in 2.38.0

- Added a crash-durable SQLite run-state store with tenant-scoped reads, transactional idempotency, bounded results, and timestamped lifecycle events.
- Documented the trust boundary: tenant IDs must come from verified authentication; SQLite supports single-node deployments and is not a distributed queue or hosted cloud service.

## What's new in 2.37.3

- Live status now shows the latest actual activity from the running job, instead of repeating text from the plan checklist.
- Home and chat status use the same current action; ongoing response generation is labeled accurately.

## What's new in 2.37.2

- Simplified the live task indicator to one quiet line with the current action and a small status dot.
- Kept the activity history available after a run, but collapsed it by default so the chat stays uncluttered.

## What's new in 2.37.1

- Reworked the live task indicator into an expandable activity card with readable actions, update counts, event icons, and timestamps.
- Improved mobile layout and light-theme contrast; added accessible activity labeling and regression coverage.

## What's new in 2.37.0

- Made the workspace overview the default Home screen, with a focused starter area and one-click routes into chat and tools.
- Added a live Home status panel that reflects the current run, action, elapsed time, plan completion, and recent activity from the local session.
- Refined spacing, hierarchy, card contrast, and small-screen layouts while keeping the existing dark/light themes and safety cues.

## What's new in 2.36.0

- Live run activity keeps up to 300 recent events and shows the retained actions throughout and after a run; long current-action text wraps instead of being cut off.
- Added guarded workspace ZIP creation with traversal/symlink checks, secret and dependency exclusions, member and size limits, and safe no-overwrite publication.
- Registered generated ZIPs in Files & Results and added safe `niji-artifact://` Markdown download links for private local downloads.
- Improved truthful live progress and completion summaries for file writes, archive creation, and local Git actions. Git push continues to use the configured local remote and credentials; this is not hosted cloud execution or a GitHub API integration.
- Added regression coverage for ZIP creation, artifact downloads, status rendering, event retention, and safety limits.

## What's new in 2.35.0

- Added a private local run journal with atomic, bounded snapshots and restart recovery; in-progress work is marked interrupted and is never replayed automatically.
- Added Run history UI/API for completed, failed, cancelled, and interrupted jobs, with saved plans, outputs, events, and explicit review-before-retry.
- Retry requires a server-validated acknowledgement before repeating side effects. Cross-thread/workspace retries use a short-lived opaque confirmation bound to the exact source and current context; unknown legacy context cannot bypass the gate.
- Workspace changes, compaction, undo, plan edits, memory updates, session switches, and provider/connector setup are serialized against active jobs to prevent state races; final run status is published together with releasing the task gate.
- Persisted job snapshots redact known secrets and credential-shaped fields, restrict file/directory permissions, reject symlinks, and cap retention/record size. Legacy run files are hardened before being read.
- Secret redaction remembers credentials observed during the UI process lifetime, including after connector/model changes, and detects secrets split across streamed chunks without delaying ordinary text. Known secrets are also masked in runtime paths, profile/artifact metadata, download filenames, and file diffs.
- Added regression coverage for file safety, crash recovery, cache retention, credential rotation, chunk-boundary redaction, repeat confirmation, and retry-context privacy.

## What's new in 2.34.0

- Added cooperative pause/resume controls for active local UI runs. Pausing takes effect at a safe model/tool boundary; an already-admitted provider/tool action may finish first.
- Pending approvals are also held at the pause boundary so approving an action does not bypass a pause request. Stop wakes a paused run and prevents not-yet-admitted tool calls from starting.
- The active job stays reserved while paused, preventing duplicate chat runs; browser reload reconnects to the same in-process job.
- Pause/resume is process-local only. If Niji exits or crashes, an active run is restored in history as interrupted but is never automatically resumed or replayed; inspect it before any manual retry.
- Added regression coverage for action-admission races, pause/resume, stop while paused, pause during approval, reconnect state, and existing approved-plan guards.

## What's new in 2.33.0

- Plan steps can now carry optional, editable acceptance criteria that remain immutable after approval.
- Approved execution must attach bounded, concrete evidence before marking a step complete; the UI and `todo_read` show the criteria and evidence, and completed evidence cannot be silently changed.
- Evidence is explicitly labeled agent-reported, not independently attested. Add and run actual tests/checks for substantive verification; the checklist alone does not prove external work occurred.
- Added regression coverage for evidence requirements, tamper resistance, storage bounds, plan editing, and safe frontend rendering.

## What's new in 2.32.0

- Added stable plan-step IDs and prerequisite dependencies with server-side validation for missing IDs, duplicates, self-links, cycles, malformed IDs, reverse-ordered prerequisites, and starting/completing a step before its prerequisites.
- The plan editor now supports adding, removing, reordering, and editing steps while preserving dependencies; progress and saved-plan views show status and human-readable prerequisite/waiting information.
- Approved execution is server-gated: only the exact approved checklist can progress, a step must be active before tools run, tool batches stay serial, and unfinished plans cannot be reported as successful. Subagent delegation is disabled during approved runs until child scope can be enforced.
- Plan storage rejects symlinked path components; POSIX uses no-follow directory descriptors and repairs private file/directory modes. Non-POSIX systems use a best-effort path-based fallback; Windows ACL parity is not verified.
- Checklist transitions are enforced, but completed status is not proof by itself that external work was substantively verified; use explicit tests/checks in the approved plan.
- Added regression coverage for dependency graph validation, approved-step transitions and execution order, incomplete runs, legacy ID migration, storage symlinks/permissions, persistence, plan edits, approval payloads, and frontend rendering.

## What's new in 2.31.0

- Added an editable plan preview: reorder or rewrite one step per line, save the proposal, then explicitly approve the exact persisted plan to run it.
- Server-side validation rejects malformed, oversized, stale, cross-thread, or already-approved plan edits; edited steps reset to pending.
- New plan-only runs clear stale checklist entries, and users may add steps to an otherwise empty preview before approval.
- Added persistence, exact-approved-content, stale-plan, malformed-input, empty-preview, and duplicate-action regression tests.

## What's new in 2.30.0

- Plan approval now starts from the server-side saved plan only after confirming it is complete, unchanged, and belongs to the active thread; stale, cross-thread, and duplicate approvals are rejected.
- Workspace status and Files & Results access now follow the agent's active workspace, not an unrelated process working directory.
- Plan preview parsing no longer treats numbered prose embedded after unrelated text as an executable plan.
- Added regressions for plan approval binding, stale/duplicate/cross-thread rejection, active-workspace artifact scope, and plan parsing.

## What's new in 2.29.0

- Session-persistent, owner-only task plans with bounded, validated step state.
- Plan-only proposals show numbered steps and require an explicit “Approve & run plan” action before execution.
- Browser UI shows and restores live task-step progress alongside the execution timeline.
- Refined the local workspace palette and added clear plan-state styling.
- Strengthened Niji's original evidence-first, plan/execute/verify, delegation, and uncertainty-handling guidance.

## What's new in 2.28.0

- Added a Files & Results workspace for the active thread, showing files changed by Niji with diff previews and one-click downloads.
- Restricted downloads to current-workspace files, regular files only, with symlink/path-traversal checks and a 10 MB limit; outside-workspace paths are omitted.
- Added UI and backend regression tests for workspace scoping, symlink rejection, and downloads.

## What's new in 2.27.0

- Added a loopback-only Automations page to schedule one-off or repeating tasks, pause/resume them, run now, and delete them.
- Added a local scheduler that runs only while the Niji UI process is running. Schedules are persisted in `~/.niji/automations.json` with owner-only permissions; intervals are bounded from 15 minutes to 30 days and at most 50 automations are stored.
- Scheduled tasks default to plan-only (no tools). If execution is enabled, existing tool approval settings still apply. Runs use the active local workspace, model, and current thread context; do not schedule sensitive prompts on a shared/untrusted device.
- Added regression coverage for UI controls, persistence/permissions, validation, and scheduled job completion.

## What's new in 2.26.0

- Added a browser model picker for configured providers: fetch a provider's model catalog or enter a model ID, run a provider chat test, and only then switch the active session and local configuration.
- Added pin/unpin controls for saved threads; pinned threads appear above recent threads. The pin list is stored locally with owner-only file permissions.
- Preserved the existing concise live task status and execution timeline, including tool-level progress, elapsed time, streamed output, stop/cancel, and reconnect behavior.
- Added regression coverage for model selection, credential redaction, failed model tests, and pin persistence/validation.

## What's new in 2.25.0

- Replaced the generic live “Thinking…” label with the truthful “Preparing the next step…” stage in the browser UI and CLI; private chain-of-thought remains hidden.
- Show the specific current action for supported tools (web search, file/document reads, edits, tests, commands, delegated tasks, memory, and more) with a concise status summary.
- Surface elapsed time on long-running command/test progress updates so users can see the task is still active.
- Added regression coverage proving live tool-start and long-running progress events reach the UI job endpoint.

## What's new in 2.24.0

- Added bounded, read-only extraction from PDF, Word, PowerPoint, and Excel files so Niji can use technical specs and project documents as context. Office XML is size/member-limited; PDF extraction is available with the `documents` extra. Extracted text is explicitly treated as untrusted data; scanned-image OCR is not included.
- Fixed custom-provider setup failures so a rejected test returns actionable guidance instead of crashing or changing saved settings.
- Hardened public fetches: hostnames are resolved and checked, TCP connects to the validated IP (preventing DNS-rebinding between validation and connect), proxy environment settings cannot hide the destination, and every redirect is revalidated.
- Added multi-version automated CI for the full unittest suite, syntax compilation, package build, and installed CLI version consistency; fixed a PTY test race that could stop reading the terminal-restoration marker mid-value.

## What's new in 2.23.0

- Added a live, request-scoped execution timeline to the chat home: safe thinking/progress summaries, tool starts/completions, compaction, approvals/denials, and errors remain visible with streamed response output. It shows actions and outcomes, never raw tool arguments or private chain-of-thought.
- Added Settings-based Nango connector management: test and authorize a connector, discover tools before saving, add/remove it live without restarting, and keep credentials out of state responses. Secrets are persisted via the existing owner-only `~/.niji/mcp.json` path.
- Exposed automatic context compaction and an estimated-token threshold in Settings; preferences persist locally. Manual compact remains available, and one emergency non-model trim on HTTP 413 remains enabled even when proactive compaction is off.
- Added regression tests for job-scoped execution events, connector secret handling/lifecycle, and saved automatic-compaction preferences.

## What's new in 2.22.0

- Added bounded interoperability with `AGENTS.md`, `CLAUDE.md`, `HERMES.md`, `.cursorrules`, and GitHub Copilot project instructions. Guidance is size-limited, scoped from repository root to the active folder, and explicitly treated as untrusted project context.
- Added lazy-loaded `SKILL.md` discovery for project and user skill folders, with bounded descriptions and safe named reads. Skills guide workflows but cannot override safety policy or the user's request.
- Fixed workspace profiles so relative file, search, Git, test, package, database, archive, process, and shell paths resolve against the active workspace in both browser and CLI flows; delegated agents inherit the same workspace.
- Added scoped, bounded `explore` (read-only), `plan` (read-only plan), and `coder` delegated-agent roles. Tool scopes are enforced at execution time, not only hidden from the model's catalog; recursive delegation remains disabled.
- Improved cancellation and subprocess cleanup, including terminating leftover child processes that could keep output pipes open after a shell exits. Tool progress remains visible in CLI/browser, and failures/non-zero results are reported as failures rather than successes.
- Expanded reliability regression coverage for skills/instructions, scoped roles, workspace switching and path resolution, interrupted tool-call history, subprocess cancellation, child-process cleanup, UI polling, and tool error states.
- Independently applied public workflow patterns from [Hermes prompt assembly](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/developer-guide/prompt-assembly.md), [Codex](https://github.com/openai/codex), [Claude Code's public plugins/examples](https://github.com/anthropics/claude-code), and [Kimi Code's agent docs](https://github.com/MoonshotAI/kimi-code/blob/main/docs/en/customization/agents.md). Kimi's current repository is MIT-licensed; this release does not copy hidden prompts or vendor their code. Kimi's separately documented skills source is in an archived predecessor repo.

## What's new in 2.21.0

- Added authenticated Streamable HTTP MCP support, including Nango's documented `/proxy/v2/mcp` endpoint, API-key bearer auth, provider-config key, connection ID, session headers, JSON/SSE responses, timeouts, safe errors, and secret redaction.
- Added guided `niji connectors add nango`, plus `list`, `test`, and `remove` commands. Nango credentials are stored only in `~/.niji/mcp.json`, with owner-only directory/file permissions; HTTP MCP credentials can also use `${ENV_VAR}` references.
- Preserved existing local stdio MCP servers. Connector failures remain isolated so one unavailable integration does not stop Niji.
- Nango advertises 1,000+ API/MCP integrations, but its hosted Free plan has capped usage and may change. Nango source is published under Elastic License 2.0 (source-available, not an OSI-approved open-source license); review the license and current limits before production use.
- Added HTTP/SSE MCP, auth headers, environment-variable references, redaction, URL validation, and stdio compatibility tests.

## What's new in 2.20.0

- Fixed the browser chat hiding all streamed model text until the request completed; current streamed output now appears under the live activity indicator.
- Kept active requests attached through temporary job-polling/network errors with capped exponential backoff, a reconnect status, and Stop still available.
- Classified MCP connector failures as tool errors in both browser activity and CLI output instead of false successful completions.
- Validated malformed tool-call IDs, indices, names, and arguments; malformed argument JSON now produces a safe tool-level error while preserving valid assistant/tool message structure.
- Stops offering more tools after the request's tool-call budget is exhausted, while allowing the model one tool-free turn to summarize completed work.
- Keeps the browser monitor recoverable after unexpected polling/render errors and reports a stop as cancelled when the provider exits by raising during cancellation.
- Added regression coverage for stream visibility, poll recovery behavior, connector failures, and malformed tool arguments.

## What's new in 2.19.0

- Expanded the simple live indicator to show both the current phase and a concise user-facing summary of what Niji is doing, including specific tool categories such as searching, reading files, editing, and running tests.
- Keeps hidden internal chain-of-thought private; status text is a short progress summary based on safe activity events. Completed answers appear normally after work finishes.
- Added regression checks for live summary details.

## What's new in 2.18.0

- Removed the Chat/Ready strip from the chat page to keep more room for conversation.
- Added the requested composer toolbar: bounded non-secret text/code attachments plus authenticated, short-lived PNG/JPEG/WebP image uploads for vision-capable provider/models (4 MiB each, 8 MiB total); image bytes are never written to session history. Also includes a public GitHub repository reference helper, a read-only Low/locked provider indicator, and optional browser speech dictation.
- Kept Send/Stop behavior and mobile-friendly layout; unsupported microphone browsers show a clear message. Private GitHub repositories are not authenticated by this helper.
- Added regression checks for the new controls and hidden chat header.

## What's new in 2.17.0

- Removed the framed chat container and the boxed backgrounds/borders around both user and Niji messages for a cleaner, open chat layout.
- Preserved readable You/Niji labels, alignment, copy action, composer, and the simple live-work status.
- Added regression checks for borderless messages and container in both dark and light themes.

## What's new in 2.16.0

- Simplified the in-progress chat display to a plain one-line status with a small spinner—no assistant message card, Copy header, border, or duplicate live text while work is running.
- The line shows only the current action, such as Thinking, searching, reading, or running tests. On completion it turns into the normal Niji reply card.
- Added UI regression checks for the simple active-work view and the normal completed-message transition.

## What's new in 2.15.0

- Simplified live work updates to show only the current activity—Thinking, planning, searching, reading, running tests, or the active tool—with no provider/model/turn metadata or duplicate streamed text while work is in progress.
- When the task finishes, the activity line clears and the completed assistant answer appears in the chat. Errors remain visible and actionable.
- Added regression checks for concise, task-specific live status and clean final-response rendering.

## What's new in 2.14.0

- Replaced the separate full-width progress strip with an inline assistant response card matching the supplied reference: Niji identity and Copy action above a live Thinking/Mapping phase, active model, and turn number.
- The composer’s Send button becomes Stop while a request is running, then returns to Send when it ends; cancellation status stays in the same assistant card.
- Scoped progress updates to the active message card so old responses are never overwritten, and corrected the Settings chat-preferences styling selector.
- Added regression checks for the inline progress card and composer Send/Stop state.

## What's new in 2.13.0

- Simplified the main workspace to a focused chat: session metrics, activity, file changes, overview, and the tool catalog now live under Settings, with collapsible sections for less clutter.
- Moved planning mode, chat export, appearance, and interaction preferences into Settings. Theme, Enter-to-send, and plan-first preferences are saved in this browser; Ctrl+, opens Settings and Ctrl+K starts a new thread.
- Kept live progress and cancellation reachable in the focused chat, while removing secondary panels and controls from beneath the chat composer.
- Added UI regression checks for the focused chat and consolidated settings.

## What's new in 2.12.0

- Live browser progress now uses readable phases such as “Thinking on it”, “Mapping it out”, tool activity, retry, and completion; assistant text streams into the chat as it arrives.
- Added a cooperative Stop control, a true plan-only mode that sends no tools, per-tool ask/allow/block/default policies, and a “Run this plan” follow-up action.
- The chat view lists the session’s recent file edits and offers guarded undo; saved/current chats can export visible user/assistant messages only.
- Settings now include local memory management and offline context compaction. Mobile layout, saved-thread search/resume, test-running tools, and Git review tools remain available.
- Added browser/API tests for streaming state, planning without tools, tool policies, transcript privacy, memory, and context compaction.

## What's new in 2.11.0

- Rebuilt `niji ui` as a complete responsive workspace inspired by the supplied mobile references: a clear navigation sidebar, chat, recent-thread search, overview, tool catalog, activity timeline, and settings.
- Start and switch browser threads, inspect detailed tool permissions and runtime/session metrics, search tools, refresh activity, and toggle dark/light appearance. Chat keeps the composer easy to reach on narrow screens and includes starter prompts, copy-response actions, and clear progress states.
- Browser tool confirmations show the action preview and allow one-time approve/deny. Approval mode can be changed for the current UI session; auto-approval warns first. Provider credentials are never sent to the page.
- Added regression coverage for dashboard routes, the state schema, new/saved sessions, and approval settings. The UI remains protected by a private launch token and bound to loopback; it is not an internet/public or LAN phone-remote-access service.

## What's new in 2.10.0

- Added `niji ui`: a token-protected browser chat/dashboard on loopback only. It shows provider/model, usage, activity, tools, and recent chat, and runs requests in the background so the browser stays responsive.
- `niji ui --open` can try to open the local page automatically. `niji ui --port 0` selects an available port. `--auto-approve` is an explicit opt-out from per-action approval; do not use it on an untrusted workspace.

## What's new in 2.9.0

- Expanded the built-in toolset with public web search, optional Playwright browsing, allowlisted Git operations, bounded test runs, literal filename/content search, precise undoable patches, package checks/installs, read-only SQLite queries, credential-free public HTTP GET/HEAD, safe ZIP/TAR inspection/extraction, and session-scoped process management.
- Read-only lookups can run without prompts under `--ask`; edits, installs, Git mutations, browser interactions, archive extraction, and process start/stop require confirmation in `--ask` mode. Database writes and private-network HTTP targets are blocked.
- Browser support is optional (`pip install 'niji-agent[browser]'` plus `playwright install chromium`); on Termux, use a trusted browser MCP connector if local Chromium is unavailable.

## What's new in 2.8.5

- `--max-tool-calls` CLI budget can now be raised to 1,000 per user request (still opt-in; default remains 30). Model turns and tool calls within each model turn remain separately capped. Connected MCP tools determine which integrations are actually available.
- Serialized concurrent activity/tool output and streamed model text literally with control characters stripped, preventing tool/status output from flickering or corrupting the pinned chat area.

## What's new in 2.8.4

- Restored the full Niji command-center dashboard at interactive launch, above the fixed chat composer, matching the supplied reference: profile, agent overview, available tools, tool usage, system status, recent activity, and quick commands. `/status` redraws it during a session.

## What's new in 2.8.3

- Interactive chat starts with the Niji-branded home area above the pinned composer; `/status` opens the full command-center dashboard.

## What's new in 2.8.2

- Fixed `/exit` cleanup for the pinned chat UI: restore full-screen scrolling, bracketed-paste/cursor/autowrap terminal modes, clear the visible dashboard while preserving terminal scrollback, and leave the shell prompt at a clean top-left position.
- Added regression tests for terminal-mode restoration, visible-screen cleanup, scrollback preservation, and invoking cleanup on chat exit.

## What's new in 2.8.1

- HTTP 413 now triggers one bounded, offline context compaction attempt and one retry. It keeps the active user request, summarizes earlier turns without another API call, trims oversized old tool results, and never retries the same oversized request unchanged.
- Forced `/compact` now actually compacts short transcripts when older turns exist, and starts at a user-turn boundary so it does not leave orphan tool results.
- `/context` shows an approximate message-size breakdown to help diagnose context errors; the chat footer now shows estimated current context separately from cumulative tokens used.
- Model switching labels distinguish the active session model from a model merely saved for that provider. A rejected 401/403 now explicitly says the switch did not occur and names the model still active.

## What's new in 2.8.0

- Reversible file edits: Niji keeps an in-memory, session-local checkpoint before its own `write_file`/`edit_file` actions (up to 1 MB per previous file). `/undo` asks before restoring and refuses if the file has changed since the checkpoint. New files can be removed by undo. Snapshots are not written to disk or session transcripts.
- Long-term memory is user-manageable with `/memory show`, `/memory add <note>`, and confirmed `/memory clear`; help warns not to save secrets.
- Search saved conversations with `/sessions <words>` or `niji sessions search <words>`.
- Undo checkpoints and restore events appear in the activity feed.

## What's new in 2.7.6

- The assistant is instructed to handle ordinary, harmless requests without generic refusals, interpret Hinglish/typos from context, and use public web sources for live/trending questions when available. It must disclose lookup failures and never claim an unperformed search.

## What's new in 2.7.5

- Fixed a chat-exit traceback after backspace: an editing cursor variable was shadowing the saved terminal settings. Added PTY regression checks that terminal echo/canonical settings are restored after editing.

## What's new in 2.7.4

- Fixed the input caret being shifted three columns to the right inside the “Ask anything” composer. It now starts before the placeholder and tracks the typed-text cursor exactly.

## What's new in 2.7.3

- User turns now print in the chat scrollback above the pinned composer instead of disappearing when the input resets.
- Left/right cursor editing, backspace, and forward-delete operate on whole visible characters/grapheme clusters, including emoji and combining-script text; cursor placement is measured in terminal cells.

## What's new in 2.7.2

- Fixed duplicate/stacking chat frames by redrawing the editor with absolute cursor positioning and clearing its exact rows.
- Reserved a fixed bottom panel for the composer and live session details; model responses scroll in the region above it. Normal terminal scrolling is restored when the interactive chat exits.

## What's new in 2.7.1

- Fixed the fresh-install crash in the branded chat composer by declaring `wcwidth` as an explicit runtime dependency; the composer imports it directly for correct terminal-cell width handling.
- Installer smoke-check now imports the chat composer before launching, so a missing UI dependency is caught during installation instead of after it.

## What's new in 2.7.0

- Branded inline chat composer inspired by the supplied reference: cyan/violet Niji frame, focused message input, and a live session-details strip below it.
- Footer shows the active model/provider, context or token usage, agent, Python runtime, tool count, and latest request/session time; fields wrap cleanly on narrow Termux screens.
- Editable terminal input supports cursor movement, history, delete/backspace, common Ctrl shortcuts, and bracketed clipboard paste.

## What's new in 2.6.0

- Provider setup and `/model` use an unbuffered terminal-byte picker that handles CSI and SS3 arrow sequences used by Android/Termux terminals; `j/k`, Page Up/Down, Enter and Esc/q are supported. Non-interactive terminals fall back to typing the provider/model name.
- Provider failures now give recovery steps: correct bad model/routes (400/404), replace credentials (401), check permissions (403), compact oversized context (413), and distinguish rate limiting from exhausted quota (429).
- SDK hidden retries are disabled. Niji performs at most one retry for transient connection/timeout/rate/server errors, adds jitter, respects short `Retry-After` hints, and defers rather than retrying early after long provider delays. Invalid-key/model/permission errors and quota/billing failures are not blindly retried.
- Bounded execution defaults: 20 model turns, 30 tool calls per user request, at most 6 tools per model turn; subagents get tighter caps. Shell commands are capped at 120 seconds, file reads at 1,000 lines, and fetched page output at 15,000 characters. `/limits` shows the active budgets; `--max-turns`, `--max-tool-calls`, and `--max-tool-calls-per-turn` can lower or raise them within hard caps.
- Failed initial provider prompts are removed from saved chat history so a corrected retry does not append a malformed consecutive user message; completed tool actions are retained and reported if a later model request fails.
- Live activity now shows retry/limit events in the dashboard and `/activity` feed.

Research references: [OpenAI API error codes](https://developers.openai.com/api/docs/guides/error-codes), [rate limits and retry guidance](https://developers.openai.com/api/docs/guides/rate-limits), [OpenAI Python SDK retry settings](https://github.com/openai/openai-python#retries), and [Python terminal cbreak mode](https://docs.python.org/3/library/tty.html).

## What's new in 2.5.1

- Model activation probes the selected chat model and applies it to the active session only when accepted (or after explicit confirmation for a non-auth probe failure)
- Live phase feed reports thinking, tool start/completion, errors and response completion; `/activity` shows the recent event history

## What's new in 2.5.0

- `/model` interactively browses preset and custom providers, fetches the selected provider's available model IDs, tests the choice, then switches and saves it without leaving the chat
- `/models` and `niji models [provider]` show full accessible catalogs when the provider exposes a compatible models endpoint; unconfigured providers are marked, and manual model entry remains available
- `/approval [ask|auto]` toggles tool confirmation during a session; `ask` is confirmation, not a security sandbox
- Loads the current workspace's `AGENTS.md` as project-specific guidance and reminds the agent to inspect diffs and run relevant checks after edits

## What's new in 2.4.1

- Groq defaults to active `openai/gpt-oss-120b`; the retired `llama-3.3-70b-versatile` saved model is migrated automatically
- Provider-specific Groq 404/401 guidance separates retired-model errors from invalid key/account authorization

## What's new in 2.4.0

- Screenshot-inspired command-center dashboard: agent profile, actual tool catalog and per-session tool use, recent activity, live session status, and quick commands
- Niji's own cyan/blue identity and original wordmark; wide side-by-side panels and mobile stacked layout
- Tool calls, uptime, and task activity are real session metrics; unavailable Skills/CPU/RAM statistics are not fabricated
- API-key input defaults to visible typing for reliable Termux use, with optional hidden mode

## What's new in 2.0

- First-run provider setup with connection test and model selection
- Original Niji terminal identity: a responsive cyan/violet/amber dashboard, custom ribbon mark, session overview, active-tools panel, and mobile-friendly stacked layout
- Live chat controls and `/help`, `/status`, `/tools`, `/setup`, `/doctor`, and `/clear` commands
- NVIDIA NIM preset with the verified GLM model ID `z-ai/glm-5.3-flash`
- Add, list, switch, and remove custom OpenAI-compatible providers
- `niji doctor` checks the saved provider and connector configuration
- Keeps task planning, subagents, MCP tools, persistent memory, resumable sessions, streaming, token counts, and context compaction
- More restrictive local permissions for saved config/session/memory data; child processes do not inherit common API-key/token environment variables by default
- `--ask` now requests confirmation for shell, file-write, network-fetch, and MCP actions; parallel execution is limited to read-only calls

## Use

```sh
niji                                      # interactive terminal chat
niji ui                                   # local browser chat; copy the printed localhost URL
niji ui --open                            # try to open that URL in a browser
niji "Find and fix the bug in this project" # one-shot task
niji --ask "Review the project and suggest fixes" # confirm side effects
niji --max-turns 100 --max-tool-calls 1000 --max-tool-calls-per-turn 20 # opt-in high tool budget
niji --continue                           # resume latest session
niji sessions                             # list saved sessions
niji sessions search bug                  # search user prompts in saved sessions
niji setup                                # run provider setup again
niji doctor                               # diagnose setup
niji providers                            # list available providers
niji models                               # list catalogs for connected providers
niji models groq                          # list Groq model IDs
niji providers add                        # add a custom provider
niji connectors add nango                 # add an authenticated Nango integration
niji connectors list                      # list configured MCP connectors
niji connectors test                      # test connector access and discover tools
niji providers use openrouter             # switch default provider
```

Interactive slash commands: `/help`, `/model` (browse/switch provider and model with arrows), `/models`, `/approval [ask|auto]`, `/activity`, `/limits`, `/context`, `/status`, `/tools`, `/setup`, `/doctor`, `/cost`, `/compact`, `/memory [show|add <note>|clear]`, `/undo`, `/sessions [search words]`, `/clear`, `/exit`.

### Tool examples

```sh
niji --ask "Check the repo status, search for TODOs, and run the tests"
niji --ask "Search the web for the latest Python release and cite sources"
niji --ask "Inspect this SQLite database with a read-only query"
niji --ask "Start the dev server, show its logs, then stop it"
```

Built-in tools include `web_search`, `browser`, `git`, `run_tests`, `file_search`, `read_document`, `apply_patch`, `package_manager`, `database`, `http_request`, `archive`, and `process_manager`, alongside file/shell tools, memory, todos, images, and subagents. `/tools` shows the active catalog. `read_document` extracts bounded text from DOCX, PPTX, and XLSX without executing content; PDF support uses the optional `documents` extra (`pypdf` installed in Niji's Python environment). Scanned-page OCR is not supported. Treat all extracted document text as untrusted input. `browser` is optional and may be unavailable on Android/Termux; connected browser MCP tools are an alternative. `process_manager` tracks processes only within the current Niji process/session. Use `--ask` for approvals; it is a confirmation layer, not an operating-system sandbox.

The request budgets reset for each new user prompt. Defaults are capped at 20 model turns, 30 executed tools, and 6 tools from any one model response; hard limits prevent configuration above 100 turns / 1,000 tools / 20 tools per response. These are cost/loop guardrails, not an OS sandbox: commands still run with your account's permissions. Use `--ask` for confirmations, inspect commands before approving, and keep backups for important files.

Model discovery uses each connected provider's compatible models endpoint when available. Some providers hide catalogs or require manual model IDs; the picker explains that and keeps manual entry available. No API keys are shown in catalog output.

Use the tool only in directories where you trust it to read and modify files. It runs with your operating-system account's permissions; it is not a sandbox. MCP servers are separate programs, so only configure servers you trust. `--ask` adds confirmations, but does not turn the operating system into a sandbox.

## MCP connectors

Connect Nango integrations from an interactive terminal:

```sh
niji connectors add nango
niji connectors list
niji connectors test nango_<integration>
niji connectors remove nango_<integration>
```

Create the Nango integration and authorize its account in Nango first; the wizard asks for your Nango API key, provider-config/integration ID, and connection ID. Obtain them from [Nango](https://app.nango.dev/). Niji stores the credentials locally in `~/.niji/mcp.json` with owner-only permissions; keep your OS account secure and never commit that file. Restart Niji after adding or removing a connector. Use `/tools` to see discovered tools. Under `--ask`, MCP actions participate in the normal approval flow.

Nango's cloud MCP endpoint is authenticated using documented bearer, provider-config, and connection-ID headers. Niji also supports `transport: "http"` MCP servers and `${ENV_VAR}` references for secret fields/headers if you prefer keeping secret values out of the JSON file. The HTTP transport currently supports Streamable HTTP JSON and server-sent-event responses. Only configure endpoints you trust; redirects are not followed.

Existing local stdio MCP configuration remains supported. Common credential variables from the parent environment are not inherited by default:

```json
{
  "servers": {
    "github": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": {"GITHUB_TOKEN": "your-token"}
    },
    "nango_linear": {
      "transport": "http",
      "url": "https://api.nango.dev/proxy/v2/mcp",
      "api_key": "${NANGO_SECRET_KEY}",
      "provider_config_key": "linear",
      "connection_id": "your-connection-id"
    }
  }
}
```

## License

MIT. See `LICENSE`.
