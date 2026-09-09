# AGENTS.md

This is the shared, tool-agnostic guide for working in this repository — for any AI coding agent (Claude Code, Cursor, GitHub Copilot, Gemini CLI, or others) and for humans. It is the single source of truth; tool-specific files (`CLAUDE.md`, `GEMINI.md`, `.cursorrules`, `.github/copilot-instructions.md`) point here rather than duplicating content.

## Architecture

Clevis is a GitHub analytics dashboard with three independently deployable services and one shared Python library:

- **`apps/api`** — FastAPI REST backend (Python). Handles auth (password + GitHub OAuth), org/membership management, analytics, RBAC, and job enqueueing. Uses SQLAlchemy 2 + Alembic against PostgreSQL.
- **`apps/worker`** — Standalone Python process. Polls the `jobs` table using `SELECT … FOR UPDATE SKIP LOCKED` and calls the GitHub API to execute background tasks (currently: clearing Actions cache). Uses raw psycopg3, not SQLAlchemy. A background thread (`_JobHeartbeat`) touches `jobs.heartbeat_at` every ~10s while a handler runs, so `_reclaim_stale_jobs` can tell a slow-but-alive job apart from a crashed one.
- **`apps/ui`** — Next.js 15 / React 19 frontend. Uses TanStack Query for data fetching, Tailwind v4, Base UI primitives, shadcn components.
- **`packages/checks`** — `clevis-checks` Python package. Defines `Check` base class and six GitHub security checks (org MFA enforcement, branch protection, secret scanning, Dependabot alerts, code scanning alerts, default-branch force-push protection). Must be installed editable for `import checks` to work.

### Database models (`apps/api/src/core/db.py`)

Tables managed by Alembic — no runtime DDL. (Not an exhaustive list of every table in `apps/api/src/core/db.py`; see that file for the authoritative schema.)
- **`users`** — email/password or GitHub-OAuth-linked accounts. `is_workspace_admin` (instance-level, set once at first-run `/auth/setup`), `token_version` (bumped to invalidate all issued JWTs), `email_verified` / `email_verify_token` / `email_verify_token_expires_at` (issue #217 — self-registered accounts start unverified and can't accept org invites until they click the emailed link; GitHub-linked and first-run-setup accounts are verified immediately since their email is already trusted).
- **`orgs`** — a GitHub org becomes a Clevis `Org` row once someone connects it, paired 1:1 with a `kind="org"` **`tenants`** row.
- **`tenants`** / **`memberships`** — every org (and every user, personally) has a `Tenant`; `memberships` is the tenant-scoped `(tenant_id, user_id) -> role ("member"|"admin")` join table `require_org_role` checks against (via `tenant_repo.get_membership`). The legacy org_id-keyed `org_memberships` table it superseded was dropped in migration 0045 / issue #331; `org_membership_repo` is now a thin org_id→tenant_id adapter over `tenant_repo`.
- **`invitations`** — pending org invites by email; `accept_invitation` requires the accepting user's email to match and (per #217) `email_verified=True`.
- **`github_installations`** — account login, installation ID, auth mode, and `token_ref` (a symbolic reference like `tok_acme`, not the actual token). Exactly one of `org_id` / `owner_user_id` is set (org-connected vs. personal installs).
- **`saved_tokens`** — legacy Fernet-encrypted PAT-per-org fallback, used when no GitHub App installation covers that org.
- **`audit_logs`** — immutable audit trail; every significant action (cache clear, dry-run, etc.) writes here with actor, action, target, and payload JSON.
- **`jobs`** — job queue; composite index on `(status, job_type)` for efficient worker polling. Status lifecycle: `queued → processing → done/failed`. The `result` column stores JSON on success or a raw exception string on failure. `retry_count` caps both reclaim-after-crash and transient-failure retries at `MAX_RETRIES`; `heartbeat_at` (issue #215) lets a long-running-but-alive job survive the reclaim sweep past `RECLAIM_TIMEOUT_MINUTES`.
- **`scan_results`** — historical security-scan snapshots (score, checks JSON) powering the score-trend chart; `scanned_by_user_id` scopes personal-endpoint scan history when there's no org membership to gate on.
- **`app_config`** — DB-backed, Settings-page-editable runtime config (`worker_poll_seconds`, `gap_heal_poll_seconds`, `gap_heal_stale_hours`; see Development setup below).
- **`webhook_deliveries`** — issue #191/S3: durable landing spot for verified GitHub webhook payloads (raw `bytea` body, delivery id, event type, resolved `tenant_id` when resolvable) before they're queued onto Redis Streams for later processing. `status` (`queued`/`queue_failed`) lets a future sweep re-enqueue anything the queue write itself failed on. Not deduplicated by `delivery_id` here — GitHub redelivers on retry, and dedupe is the S4 event-processor's job, not this table's.

### Job queue flow

**Enqueueing (API):** `POST /repos/{owner}/{repo}/actions-caches/clear` — if `dry_run=true`, only an audit log is written; if `dry_run=false`, the token is Fernet-encrypted and a `jobs` row with `status='queued'` is inserted.

**Processing (worker):** atomic `SELECT FOR UPDATE SKIP LOCKED` claims one job, updates status to `processing`, decrypts the token, calls the GitHub API, then updates to `done` or `failed`. Multiple worker replicas can poll safely.

### RBAC

Access is enforced with JWT session auth, not an `X-Role` header:

- `require_auth` / `require_workspace_admin` — `apps/api/src/core/auth.py` (any signed-in user vs instance workspace admin).
- `require_org_role("member"|"admin")` — `apps/api/src/core/rbac.py` (org-scoped membership lookup in the DB, against the tenant-scoped `memberships` table via `tenant_repo.get_membership`).

The old `viewer` / `analyst` / `admin` header model was removed in Phase 5.

### Auth & GitHub App

Two sign-in paths, both issuing the same JWT session: password (`/auth/setup` for the first-run admin, `/auth/register` + `/auth/login` after that) and "Sign in with GitHub" OAuth (`apps/api/src/routers/github_auth.py`). Separately, a **GitHub App installation** (`apps/api/src/routers/installations.py`, `apps/api/src/routers/webhooks.py`) is how an org actually grants Clevis API access — installing lets the API mint short-lived per-installation tokens instead of relying on a browser-pasted PAT. `POST /webhooks/github` (HMAC-signature-verified) keeps `github_installations` in sync on install/uninstall lifecycle events; it does not do full webhook-driven event ingestion.

### Token encryption / storage

GitHub credentials may be stored as Fernet-encrypted rows in `saved_tokens` (legacy PAT path). Separately, when a job is enqueued the API Fernet-encrypts the token for the worker payload (key derived via SHA-256 of `JOB_SECRET_KEY`, base64-encoded); the worker decrypts at processing time. Prefer a connected GitHub App installation so the API can mint short-lived installation tokens via `token_resolution` instead of a browser-pasted PAT. Shared crypto lives in `packages/checks` (`checks.crypto`); thin wrappers remain in api/worker `_crypto.py`.

### Database URL dialect

The API uses `postgresql+psycopg://` (SQLAlchemy dialect). The worker strips the `+psycopg` prefix to obtain a plain `postgresql://` URL for psycopg3's native `connect()`.

### Analytics & checks integration

`analytics_service.py` calls `checks.runner.run_all_checks(owner, token, base_url)`, which instantiates and runs the six `Check` subclasses in `packages/checks/src/checks/github_checks.py` (`OrgMFARequired`, `BranchProtectionEnabled`, `SecretScanningEnabled`, `DependabotAlertsCheck`, `CodeScanningCheck`, `DefaultBranchNoForcePushCheck`). Score is computed as `100 - (failed_count / total_count * 100)`. Each check returns `{"status": "pass"|"fail", "value": <object>}`. The checks package handles GitHub API pagination internally via Link header parsing, and retries GitHub's primary (429) and secondary (403 + rate-limit headers) rate limits — callers receive complete results. Each scan is also persisted to `scan_results` for the score-trend chart.

### Request ID propagation

`RequestIdMiddleware` (`apps/api/src/core/middleware.py`) assigns a UUID to every request (or forwards `X-Request-ID` if present), stores it in a `ContextVar`, and injects it into all log records. The ID is echoed in the `X-Request-ID` response header.

### UI routing

Owner and repo are joined with `~` in URL dynamic segments (e.g., `/repos/octocat~hello-world/cache`). The API client lives in `apps/ui/lib/api/client.ts`; it centralises JSON serialisation, error parsing, and Bearer JWT injection from the session token.

## Development setup

```bash
# One-time setup
cp .env.example .env           # fill in ALL variables — none have defaults
pip install -r apps/api/requirements.txt
pip install -r requirements-test.txt
pip install -e packages/checks
cd apps/ui && bun install
```

**Required env vars (7 total):** `DB_USER`, `DB_PASSWORD`, `DB_NAME`, `JOB_SECRET_KEY`, `AUTH_SECRET`, `NEXT_PUBLIC_API_BASE`, `REDIS_PASSWORD`. Everything else is optional with a safe default in code (`CORS_ORIGINS`, `GITHUB_API_BASE`, the `GITHUB_APP_*` block for GitHub App auth/OAuth, the `SMTP_*` block for verification emails — see `.env.example`). Everything else lives in the `app_config` DB table (configured via Settings page).

Key variables:
- `DB_USER`, `DB_PASSWORD`, `DB_NAME` — Postgres credentials. Docker Compose maps these to `POSTGRES_USER/PASSWORD/DB` for the db container; entrypoints construct `DATABASE_URL` from them (host = `db`).
- `DATABASE_URL` — local dev only (outside Docker); format: `postgresql+psycopg://<user>:<pass>@localhost:5432/<db>`.
- `JOB_SECRET_KEY` — Fernet key for token encryption; generate with `openssl rand -hex 32`
- `AUTH_SECRET` — JWT signing secret; generate with `openssl rand -hex 32`
- `NEXT_PUBLIC_API_BASE` — `http://localhost:8080` for local dev
- `REDIS_PASSWORD` — issue #191/S3's webhook ingestion queue (Redis Streams). Auths the `redis` Compose service (`--requirepass`, so any other container on the shared network can't read/inject stream entries); `apps/api/entrypoint.sh` builds `REDIS_URL` from it, same pattern as `DB_USER`/`DB_PASSWORD`/`DB_NAME` → `DATABASE_URL`. `REDIS_URL` itself is local-dev-only (outside Docker), same as `DATABASE_URL`.
- `API_PORT` / `UI_PORT` — `8080` / `3000`

**Deploy-time config (env vars, safe defaults in code):**
- `CORS_ORIGINS` — JSON array of allowed origins; default `["http://localhost:3000"]`. Read once at API startup (a security boundary), so a change requires an API restart. Set your real UI domain in production.
- `GITHUB_API_BASE` — default `https://api.github.com`; set for GitHub Enterprise (e.g. `https://github.yourco.com/api/v3`). Used by both the API and the worker. Not runtime-editable because it's where GitHub tokens are sent.
- `GITHUB_APP_ID` / `GITHUB_APP_CLIENT_ID` / `GITHUB_APP_CLIENT_SECRET` / `GITHUB_APP_PRIVATE_KEY` / `GITHUB_APP_WEBHOOK_SECRET` / `NEXT_PUBLIC_GITHUB_APP_SLUG` — unset by default (all `None`); "Sign in with GitHub" and the "Install GitHub App" button raise a clear "not configured" error until these are set. See `docs/self-hosting.md` for how to register the App.
- `SMTP_HOST` / `SMTP_PORT` (default `587`) / `SMTP_USER` / `SMTP_PASSWORD` / `SMTP_FROM` — unset by default. Issue #217: without these, self-registered accounts are still created successfully but stay `email_verified=False` and can't accept org invitations until an operator configures SMTP (or the user later verifies via GitHub OAuth linking, which verifies immediately).
- `WORKER_DB_PASSWORD` — unset by default, in which case the worker keeps sharing the API's `DB_USER`/`DB_PASSWORD` credential (today's behavior). Issue #190, step 1: when set, `docker/postgres-init/01-create-worker-role.sh` provisions a `clevis_worker` Postgres role on a fresh `db` data volume, migration `0020` grants it SELECT/INSERT/UPDATE on `jobs` and SELECT on `app_config` (the only tables the worker touches), and `apps/worker/entrypoint.sh` connects as that role instead. Prerequisite for eventually granting the worker `BYPASSRLS` once Row-Level Security lands — see `docs/self-hosting.md` for manual role provisioning on existing deployments (the init script only runs on a brand-new volume).
- `API_DB_PASSWORD` — unset by default, in which case the API connects as `DB_USER`, the `initdb` bootstrap superuser, which unconditionally bypasses Row-Level Security regardless of `ENABLE`/`FORCE` (issue #330 — this is why tenant isolation from migrations `0030`/`0031`/`0033` isn't actually enforced in a default deployment). When set, `docker/postgres-init/02-create-api-role.sh` provisions a `clevis_api` Postgres role on a fresh `db` data volume, migration `0032` grants it schema `USAGE`, table privileges on every table, and sequence privileges for every serial primary key, and `apps/api/entrypoint.sh` runs Alembic migrations under the superuser credential (DDL/GRANT privilege the new role intentionally lacks, and it must stay table owner for `ENABLE`-without-`FORCE` policies to mean anything) but serves runtime requests as `clevis_api` — see `docs/self-hosting.md` for manual role provisioning on existing deployments (the init script only runs on a brand-new volume).

**DB-backed config (editable in Settings → Instance Configuration):**
- `worker_poll_seconds` — default `5`, clamped to `[1, 30]`; the worker re-reads it each loop, so changes take effect live without a restart. The upper clamp keeps the worker's heartbeat healthcheck in `docker-compose.yml` meaningful (see `apps/worker/src/worker.py`'s `_MAX_POLL_SECONDS`).
- `gap_heal_poll_seconds` — default `900`, clamped to `[60, 3600]`. How often the API's gap-heal sweep (issue #192/S5 PR 2) runs, via an `asyncio` background loop started in `apps/api/src/main.py`'s lifespan (`src/services/gap_heal_loop.py`) — the API's own equivalent of the worker's poll loop, re-read each iteration.
- `gap_heal_stale_hours` — default `6`, clamped to `[1, 168]`. How old a tenant's `activity_sync_cursors.last_synced_at` must be before the sweep re-enqueues a `github.backfill_repo_events` job for it (`src/services/gap_heal_sweep.py`).

## Running locally

Each command runs in a separate terminal from the repo root:

```bash
# Terminal 1: Postgres
docker compose up db

# Terminal 2: API (PowerShell: run as two separate commands)
cd apps/api
alembic upgrade head
uvicorn src.main:app --reload  # http://localhost:8080

# Terminal 3: UI
cd apps/ui && bun run dev      # http://localhost:3000

# Terminal 4 (optional): worker
cd apps/worker && python src/worker.py
```

Full stack via Docker:
```bash
docker compose --profile backend --profile frontend up --build
```

Docker Compose profiles: `backend` (db + api + worker), `frontend` (db + ui), default = db only.

## Commands

### Python tests
```bash
pytest -q                                    # all tests from repo root
pytest -q apps/api/tests/test_health.py     # single file
```

Tests hit a real Postgres database — no mocks. `pytest.ini` adds `apps/api` and `apps/worker/src` to `pythonpath`. Each test function runs inside a transaction with a savepoint; the savepoint is rolled back after the test, giving a clean DB state without truncating tables per-test. Once, at the very end of the whole pytest session, `apps/api/tests/conftest.py`'s `_engine` fixture teardown does truncate every ORM table on whatever database `DATABASE_URL` points at for that test run — this exists to recover from non-test traffic (a manually-run `uvicorn`/E2E session against the same local DB) committing real rows that would otherwise make the next `pytest -q` run's `/auth/setup`-dependent tests fail with 409s. This is the one narrow, test-only exception to the "no dropping or truncating tables" guardrail below: it never runs outside a pytest session, and it's guarded fail-closed by an explicit hostname allowlist (`localhost`/`127.0.0.1`/`db` — the only hosts this repo's own dev/CI setup ever points `DATABASE_URL` at) that skips the truncate (with a warning) for any other host, so pointing a test run's `DATABASE_URL` at a real deployment can't trigger it. (CI's constrained-role test job also lacks TRUNCATE privilege by design and the fixture skips itself there too, rather than failing.)

### UI
```bash
cd apps/ui
bun run dev          # dev server
bun run typecheck    # tsc --noEmit
bun run lint         # eslint
bun run test         # vitest run
bun run check        # typecheck + lint + build
```

### Database migrations
```bash
cd apps/api
alembic revision --autogenerate -m "description"
alembic upgrade head
```

Migration files live in `apps/api/alembic/versions/`. Use zero-padded 4-digit prefixes (e.g. `0003_...`).

## Commit conventions

Conventional Commits are enforced via commitlint + husky. Format: `type(scope): subject`. Types: `feat`, `fix`, `chore`, `docs`, `refactor`, `test`, `ci`. Merge commits are excluded from linting.

**Git hooks (husky, `.husky/`):**
- `commit-msg` — commitlint against Conventional Commits.
- `pre-commit` — `cd apps/ui && bun run typecheck && bun run lint && bun run build`.
- `pre-push` — `cd apps/ui && bun run test && bun run build`, then `pytest -q` from the repo root (needs a reachable Postgres — see Running locally).

## Guardrails for AI Agents

These apply to every AI tool working in this repo, not just Claude Code.

### 1. Migrations require deliberate care, never rushed

This is the highest-priority guardrail in this file.

- Before creating a new migration file, stop and think hard about whether a schema change is actually necessary for the task — don't generate one reflexively or "just in case." Unnecessary migration files make contributors' PRs noisy and hard to review.
- If a schema change genuinely is required, run `alembic revision --autogenerate`, then read the generated diff carefully before running `upgrade head`. Autogenerate can miss renames (emits drop+add, losing data), miss server-side defaults, or pick up unrelated drift from a stale local DB.
- Never run `alembic downgrade` against a real environment without explicit user confirmation.
- Never hand-edit a migration file that has already been applied anywhere (including a teammate's local environment) — write a new migration instead.
- If a migration touches a column with existing data (type change, `NOT NULL`, drop), state the data-loss/backfill risk explicitly before running it, even if not asked.
- Before opening a PR with a new migration, check `apps/api/alembic/versions/` against latest `main` (not just your branch) for a numbering collision — two PRs developed in parallel can both claim the same next number. If `alembic upgrade head` reports multiple heads after merging main in, renumber your migration (new revision id + `down_revision` pointing at the real new head) rather than leaving the collision for whoever merges second to discover.

### 2. Don't fabricate facts

Verify before asserting. Read the actual file or grep the repo before claiming a function, config var, endpoint, or library behavior exists — don't infer from naming conventions or from how a "similar" codebase might work.

This repo has sharp edges worth double-checking rather than assuming:
- 6 required env vars have no defaults — the app hard-fails without them (see Development setup above).
- Org admin actions use `require_org_role("admin")` (DB membership), not a client-supplied role header.
- Job-token Fernet helpers should stay in sync via `checks.crypto` (api/worker `_crypto.py` are thin wrappers).

### 3. Other irreversible actions require explicit confirmation

No dropping or truncating tables against a real environment (the one narrow exception: the disposable test-DB truncate described in the Commands → Python tests section above, which only ever runs inside a pytest session against a local/CI throwaway database). No bypassing `require_org_role("admin")` on privileged org actions (e.g. cache clear). No committing `.env` or real tokens/secrets. Avoid `git push --force` to shared branches unless a maintainer explicitly asks for a history rewrite on a feature branch.

### 4. Don't scope-creep

A bug fix doesn't need surrounding cleanup. Don't touch files outside what was asked. Don't add abstractions for hypothetical future requirements. Most damage from AI agents in a mature codebase comes from unrequested "improvements," not from wrong facts — keep changes scoped to the task.

### 5. Complete setup before your first commit or push

**Mandatory:** before touching this repo, run the full One-time setup block (see Development setup above) — `pip install -r apps/api/requirements.txt`, `pip install -r requirements-test.txt`, `pip install -e packages/checks`, `cd apps/ui && bun install`. The `pre-commit` hook shells out to `bun run typecheck && bun run lint && bun run build`; without `bun install` first it fails with confusing "command not found" / missing-`node_modules` errors rather than a real typecheck/lint/build result. Before your first `git push`, also make sure a local Postgres is reachable (`docker compose up db`, with `DB_*`/`DATABASE_URL` set) — `pre-push` runs `pytest -q`, which hits a real database.

Never bypass a failing hook with `--no-verify`, `HUSKY=0`, or by editing/deleting files under `.husky/` — fix the underlying failure (usually a missing dependency or a real lint/type/test error) instead.

## AI Attribution Policy

- All AI-authored commits use a `Co-Authored-By: <Agent Name> <noreply@...>` trailer, regardless of which tool made the commit.
- PR descriptions for AI-assisted work must give a **detailed explanation of what changed and why** — not a summary of the diff, but the reasoning behind it — for every change included.
- **If a PR includes a new or modified migration file, the description MUST explicitly state that a schema change is included and explain why it was necessary.** This is non-negotiable: migrations are the highest-risk category of change in this repo (real Postgres, no rollback safety net) and must never be silently bundled into a PR.
- If a PR touches other sensitive files (`.env`/config, RBAC/auth code, token encryption logic), the description must explicitly flag this too, with reasoning.
- No inline code comments claiming AI authorship (e.g. `// generated by AI`). Attribution belongs in commit/PR metadata, not source files.
- AI-authored changes still require human review before merge. An agent's commit is not a substitute for a reviewer.
