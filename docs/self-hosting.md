# Self-hosting

This is the infrastructure/ops guide — getting the Clevis stack itself running and configured. For how to sign in and connect a GitHub org once the instance is up, see the [README](../README.md#get-started).

## Requirements

- Docker + Docker Compose

## Setup

1. Clone the repo and copy the env template:

   ```bash
   git clone https://github.com/nazarli-shabnam/clevis
   cd clevis
   cp .env.example .env
   ```

2. Fill in `.env`. Seven vars are hard-required (the app fails to start without them) — `DB_USER`, `DB_PASSWORD`, `DB_NAME`, `JOB_SECRET_KEY` (`openssl rand -hex 32`), `AUTH_SECRET` (`openssl rand -hex 32`), `NEXT_PUBLIC_API_BASE`, `REDIS_PASSWORD` (`openssl rand -hex 32` — auths the `redis` service used for webhook ingestion, issue #191). Everything else in `.env.example` has a safe default and only needs overriding per environment (e.g. `CORS_ORIGINS` for your real UI domain, `GITHUB_API_BASE` for GitHub Enterprise).

3. Register a GitHub App on github.com (**Settings → Developer settings → GitHub Apps → New GitHub App**) so users can connect their orgs after the instance is running. The App form asks for three separate URLs — each one is where GitHub sends the browser back to at a different point in the flow, so don't reuse one URL for another's purpose:
   - **Homepage URL** — your deployed UI URL. Shown on the App's public listing page; not used in any redirect.
   - **Callback URL** — `<NEXT_PUBLIC_API_BASE>/auth/github/callback`. GitHub redirects here after a user clicks "Sign in with GitHub" and approves the login. Must match this exact path, or sign-in fails.
   - **Setup URL** (under "Post installation") — `<NEXT_PUBLIC_UI_BASE>/settings/github-callback`, with **"Redirect on update"** also checked. GitHub redirects here after a user clicks "Install GitHub App" on the Settings page and finishes installing the App on an org. Setting this URL is what lets Clevis learn the installation happened: on that redirect, the UI records a new `github_installations` row for the org. If the Setup URL field is left blank, GitHub instead redirects to the Homepage URL above — Clevis never sees that callback, so the org never gets connected, and the app silently behaves as if nothing was installed at all (no error, just a permanently empty org).
   - **Webhook** — optional, but recommended: set the webhook URL to `<NEXT_PUBLIC_API_BASE>/webhooks/github`, generate a webhook secret in the same form, and copy that secret into `.env` as `GITHUB_APP_WEBHOOK_SECRET`. Without a webhook configured, Clevis has no way to find out when a user uninstalls the App on GitHub's side, so that org's `github_installations` row is never cleaned up — it just goes stale in the database.
   - **Webhook events** — subscribe to `Dependabot alert`, `Code scanning alert`, and `Secret scanning alert`, and grant the matching read-only permissions (**Dependabot alerts**, **Code scanning alerts**, **Secret scanning alerts**) under "Permissions & events". Without these, `POST /webhooks/github` never receives `dependabot_alert`/`code_scanning_alert`/`secret_scanning_alert` deliveries at all — GitHub only sends events an App is both permitted to read and subscribed to, silently, with no error on either side. These feed the Security dashboard's per-repo alert data — `apps/worker/src/event_consumer.py` normalizes them into `security_alerts` (post-S6 PR 2).
   - Also subscribe to `Member`, `Organization`, `Membership`, and `Team` — these need only the **Members** organization permission set to **Read-only** (covers all four; no Administration or write access needed). Feeds the Collaborators dashboard's org-membership/repo-access data — `member`/`organization` are normalized into `org_members`/`repo_collaborators` (post-S6 Collaborators PR 1); `membership`/`team` are durably queued but acked-and-skipped for now (team-based repo access is deferred).
   - Also subscribe to `Push`, `Pull request`, `Issues`, `Release`, and `Create` (needs the **Contents**, **Pull requests**, **Issues**, and **Metadata** repository permissions at **Read-only**). `apps/worker/src/event_consumer.py` normalizes these into `repo_events`, which powers the Activity Feed and the commit heatmap / Overview aggregates. Without them the Activity Feed still works but falls back to a rate-limited live GitHub read (only the last ~30 org events, no history), and the heatmap/aggregates stay sparse — they're only otherwise seeded by the one-time install-time backfill, which ages out.
   - Grant read access to the repository/organization data the security checks need (contents, metadata, administration, members).
   - **Optional — Actions-minutes usage card (issue #294):** to show the Overview page's "Actions Usage" card, grant the App the organization **Administration** permission at **Read-only** (this is what gates `GET /organizations/{org}/settings/billing/usage/summary`, the enhanced-billing usage API — the older `/settings/billing/actions` endpoint it used to call was retired by GitHub on 2025-09-26). Billing data was an explicitly deferred roadmap area; without this permission the card just doesn't render (the API returns a 400 and the query fails silently). No write access is needed.
   - **Optional — "File as issue" (issue #286):** to let admins open a GitHub issue straight from a failed Security check, grant the **Issues** repository permission **Read and write**. Without it the button is still shown, but the create call returns a 403 that the UI surfaces as a "needs Issues: write" hint.
   - **Optional — "Fix this" auto-remediation (issue #287):** to let admins enable a failing security setting in one click from the Security page, grant **Administration** repository permission **Read and write** (branch protection, secret scanning) and **Dependabot alerts** **Read and write**. `Fix this` currently covers: enable secret scanning, enable Dependabot alerts, and apply branch protection to the default branch — the last of which also resolves the "default branch allows force pushes" check. When the branch has *no* protection yet, Clevis applies a conservative default: require 1 PR review, block force-pushes and deletion, no required status checks, and `enforce_admins` **off** (so a repo admin keeps an emergency-merge path — tighten this yourself if you want admins bound too). When the branch is *already* protected, Clevis keeps every existing rule and only turns force-pushes off; if the existing rules include push restrictions it can't safely reconstruct, the fix returns a 409 and asks you to make the change manually. Org 2FA enforcement and code-scanning-alert dismissal are intentionally not automated. Existing installs must re-approve after the permissions are added; without them each fix returns a 400 pointing back here.
   - **Optional — Stale-PR nudges (issue #289):** to let admins nudge open pull requests that have sat without review past a threshold — a one-line comment (idempotent) or a `needs-review` label — grant the **Pull requests** repository permission **Read and write**. The threshold and mode are set in Settings → Instance Configuration: `pr_nudge_stale_days` (default 3) and `pr_nudge_mode` (`off` / `comment` / `label`, default `comment`). Without the permission the "Nudge stale PRs" button on the Pull Requests page returns a 400 pointing here. On-demand only — a periodic background sweep is a separate follow-up.
   - **Optional — Bulk branch-protection apply (issue #288):** to let an org admin apply a branch-protection preset to many repos at once from the Automation page, grant the **Administration** repository permission **Read and write** (same permission "Fix this" uses). The Automation → Branch protection card runs a dry-run diff first, then applies. The preset controls four knobs (required approvals, enforce-on-admins, block force-push, block deletion); every other rule a branch already has — required status checks, code-owner reviews, linear history, etc. — is preserved, and the diff shows exactly what would change. A branch whose protection restricts *who* can push is reported as an error and left untouched. Applied presets can be saved per repo (`automation_repo_settings`). Without the permission every repo comes back 403 and the call returns a 400 pointing here.
   - **Optional — Workflow policy lint + auto-fix PR (issue #291):** the Automation page's "Lint workflows" button scans a repo's `.github/workflows/*.yml` for a `pull_request_target` trigger that checks out untrusted PR code (and for attacker-controlled text interpolated into `run:` scripts). Scanning is read-only and needs no extra permission. **"Open fix PR"** — which flips `pull_request_target` to `pull_request` when the workflow uses no secrets — needs the **Contents**, **Pull requests**, *and* **Workflows** repository permissions at **Read and write** (GitHub blocks pushing `.github/workflows/**` changes without the last). Org-admin only; without the scopes it returns a 400 pointing back here.
   - **Optional — Dependabot auto-triage (issue #290):** the Automation page can approve (and, if you opt in, merge) low-risk Dependabot PRs. Grant the **Pull requests** repository permission **Read and write** (to approve); for `approve_and_merge` mode also grant **Contents** **Read and write** (to merge). **It's disabled per repo by default** — enable it per repo in the Automation page and pick a mode. `approve_only` (the default) only approves; only `approve_and_merge` merges, and "Run for real" is a two-step confirm. A PR is acted on only when *all* hold: the author is `dependabot[bot]`, the bump is patch-level (parsed from the PR body; unparseable → skipped), every check on the head commit is a completed success, and there's no pending or requested-changes human review. There's a per-run cap (5), a dry-run preview, and every decision — acted on or skipped, with the reason — is written to the audit log. Without the permissions the run returns a 400 pointing back here.
   - **Private key** — this is a separate secret from the webhook secret above; GitHub doesn't ask for it on the creation form. After the App is created, open its settings page, scroll to the **"Private keys"** section (near the bottom, under "General"), and click **"Generate a private key"**. This downloads a `<something>.pem` file — open it and copy the *entire* contents, including the `-----BEGIN RSA PRIVATE KEY-----` / `-----END RSA PRIVATE KEY-----` lines, into `.env` as `GITHUB_APP_PRIVATE_KEY`.
   - Copy the remaining values into `.env`: `GITHUB_APP_ID`, `GITHUB_APP_CLIENT_ID`, `GITHUB_APP_CLIENT_SECRET` (all shown on the same settings page, near the top). Also set `NEXT_PUBLIC_GITHUB_APP_SLUG` to the App's slug (found in its public page URL) — the UI uses this slug to build the "Install GitHub App" button's link.

   **If your GitHub App was registered before the Setup URL field above was documented here:** open the App's settings on github.com, go to the "Post installation" section, and add the Setup URL now. Orgs that already completed installation don't need to reinstall — only new installs and updates will use the new callback page going forward.

   **Connecting more than one account/org:** there's no limit — install the App from the sidebar menu (labeled "Connect account" before you've connected anything, "Switch account" once you have) or the Settings → Connected GitHub accounts button, as many times as you need. Connecting an organization requires you to be a GitHub **owner** of it (Clevis live-checks this against GitHub); if you're only a member, ask an owner to connect it.

   **After the App's permissions are widened:** when a new Clevis release adds an optional write automation (issues #286–#291), existing installations keep working but the new feature stays blocked until a GitHub org owner re-approves the App's updated permission request. Clevis shows affected org admins a "N automations need extra GitHub access" notice in Settings and on the Automation page with a "Review on GitHub" link; the notice clears automatically once the owner approves (via the `installation` `new_permissions_accepted` webhook).

4. (Optional) Configure SMTP so self-registered accounts can verify their email: set `SMTP_HOST`, `SMTP_PORT` (default `587`), `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM` in `.env`. Without these, registration still works — accounts are created immediately — but they stay unverified and can't accept an org invitation until either SMTP is configured and the user clicks the emailed link, or they link a GitHub account instead (GitHub-verified emails are trusted immediately). Accounts created via first-run `/auth/setup` or "Sign in with GitHub" are always verified, regardless of SMTP.

5. (Optional) Give the worker its own Postgres credential, separate from the API's `DB_USER`/`DB_PASSWORD`: set `WORKER_DB_PASSWORD` in `.env`. This is a prerequisite for a future Row-Level Security migration (issue #190) and has no effect otherwise — the worker keeps working exactly as before if left unset. It only takes effect via the `db` container's first-ever startup (`docker-entrypoint-initdb.d` scripts only run once, against a fresh, empty data volume). If you're setting this on an **existing** deployment (a `db` volume that's already initialized), run this once by hand instead:

   ```bash
   # 1. docker compose up -d db recreates the db container so it picks up
   #    WORKER_DB_PASSWORD from .env, then re-runs the same init script the
   #    fresh-volume path uses (safe to re-run; it's a no-op if the role
   #    already exists) — this avoids retyping the password into a raw SQL
   #    literal and avoids relying on host-shell vars .env doesn't export.
   docker compose up -d db
   docker compose exec db sh /docker-entrypoint-initdb.d/01-create-worker-role.sh

   # 2. Grant it the table privileges migration 0020 would otherwise apply.
   #    Runs inside the db container so it uses the container's own
   #    POSTGRES_USER/POSTGRES_DB, not unexported host-shell variables.
   docker compose exec db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "GRANT SELECT, UPDATE ON jobs TO clevis_worker; GRANT SELECT ON app_config TO clevis_worker; GRANT SELECT, INSERT ON repo_events TO clevis_worker; GRANT USAGE, SELECT ON repo_events_id_seq TO clevis_worker; GRANT SELECT, UPDATE ON webhook_deliveries TO clevis_worker;" -c "DO \$do\$ BEGIN IF EXISTS (SELECT FROM pg_tables WHERE schemaname = '"'"'public'"'"' AND tablename = '"'"'repo_event_daily_counts'"'"') THEN GRANT SELECT, INSERT, UPDATE ON repo_event_daily_counts TO clevis_worker; END IF; IF EXISTS (SELECT FROM pg_tables WHERE schemaname = '"'"'public'"'"' AND tablename = '"'"'activity_sync_cursors'"'"') THEN GRANT SELECT, INSERT, UPDATE ON activity_sync_cursors TO clevis_worker; END IF; IF EXISTS (SELECT FROM pg_tables WHERE schemaname = '"'"'public'"'"' AND tablename = '"'"'security_alerts'"'"') THEN GRANT SELECT, INSERT, UPDATE ON security_alerts TO clevis_worker; GRANT USAGE, SELECT ON security_alerts_id_seq TO clevis_worker; END IF; IF EXISTS (SELECT FROM pg_tables WHERE schemaname = '"'"'public'"'"' AND tablename = '"'"'org_members'"'"') THEN GRANT SELECT, INSERT, UPDATE, DELETE ON org_members TO clevis_worker; GRANT USAGE, SELECT ON org_members_id_seq TO clevis_worker; END IF; IF EXISTS (SELECT FROM pg_tables WHERE schemaname = '"'"'public'"'"' AND tablename = '"'"'repo_collaborators'"'"') THEN GRANT SELECT, INSERT, UPDATE, DELETE ON repo_collaborators TO clevis_worker; GRANT USAGE, SELECT ON repo_collaborators_id_seq TO clevis_worker; END IF; IF EXISTS (SELECT FROM pg_tables WHERE schemaname = '"'"'public'"'"' AND tablename = '"'"'org_membership_sync_cursors'"'"') THEN GRANT SELECT, INSERT, UPDATE ON org_membership_sync_cursors TO clevis_worker; END IF; END \$do\$;"'
   ```

   The `repo_event_daily_counts`, `activity_sync_cursors`, `security_alerts`, `org_members`, `repo_collaborators`, and `org_membership_sync_cursors` grants are guarded by existence (unlike the others on this line): this step can run before step 7 below has ever applied Alembic migrations on an update to an existing deployment, and an unconditional `GRANT` on a table that doesn't exist yet would fail outright (CodeRabbit finding on #341) — the other grants here predate this guard and are left as-is, not retroactively changed, to keep this fix scoped to what #341 actually added.

   Don't rely on re-running `alembic upgrade head` for step 2 — if migrations `0020`/`0036`/`0037`/`0038`/`0039`/`0040`/`0041` already applied (as a no-op, since the role didn't exist yet), Alembic considers them done and won't re-run them. Restart the `worker` container afterward to pick up the new credential.

6. (Recommended) Give the API its own non-superuser Postgres credential, separate from `DB_USER`/`DB_PASSWORD`: set `API_DB_PASSWORD` in `.env`. Without this, the API connects as `DB_USER` — the `initdb` bootstrap superuser — which unconditionally bypasses Row-Level Security, so tenant isolation is enforced only at the application layer (issue #330). Same fresh-volume-only caveat as the worker's credential above. If you're setting this on an **existing** deployment, run `docker/provision-api-role-existing-deployment.sh` instead of the fresh-volume init script:

   ```bash
   docker compose up -d db
   docker compose cp docker/provision-api-role-existing-deployment.sh db:/tmp/provision-api-role.sh
   docker compose exec db sh /tmp/provision-api-role.sh
   ```

   Unlike the worker (which only ever needs `SELECT`/`UPDATE` on `jobs` and `SELECT` on `app_config`), the API needs schema `USAGE`, table privileges on every table, and sequence privileges for every serial primary key — this script creates the role and applies all three grant kinds in a single `psql` invocation wrapped in one transaction, so an operator never ends up with a role that has `CONNECT` but no table/schema/sequence privileges (which a failure partway through two separate commands could otherwise leave behind). It's deliberately not placed under `docker/postgres-init/` — that directory auto-runs on every fresh volume, at a point before Alembic has created any tables yet, so its `GRANT ... ON users, orgs, ...` statements would fail on a genuinely fresh volume. Safe to re-run. Restart the `api` container afterwards to pick up the new credential.

7. Start the stack:

   ```bash
   docker compose up --build -d
   ```

   Or, on a tagged release, pull the pre-built images instead of building from source:

   ```
   ghcr.io/<owner>/clevis-api
   ghcr.io/<owner>/clevis-worker
   ghcr.io/<owner>/clevis-ui
   ```

8. Verify it's up:

   ```bash
   curl http://localhost:8080/healthz   # -> {"status": "ok"}
   ```

   Then open the UI and continue with the [Get started](../README.md#get-started) flow in the README.

## Leadership digest (optional, issue #292)

Clevis can email each org's admins a periodic summary (security score movement, current
open risk items, recent push activity). It is **off by default** and uses only data
Clevis already stores — no additional GitHub App permission.

- Requires SMTP (the same `SMTP_*` block as email verification above).
- Enable it in **Settings → Instance Configuration → Leadership Digest** (`weekly` or
  `monthly`), or set the `digest_cadence` app-config key directly.
- `digest_poll_seconds` (default `3600`, clamped `[300, 86400]`) controls how often the
  API's background sweep checks whether a digest is due; a tenant is emailed at most once
  per cadence interval, tracked via a `digest.sent` audit-log entry.

## Security notes

- Sign-in uses a JWT held in an httpOnly session cookie, not a trusted request header — set `session_cookie_secure=false` only for local HTTP dev, never in production.
- Restrict API ingress behind your reverse proxy/SSO; the base `docker-compose.yml` deliberately publishes no host ports (Traefik-only) for this reason.
- Prefer GitHub App auth over the legacy personal-access-token path for anything beyond local testing — tokens are Fernet-encrypted at rest either way, but App installation tokens are short-lived and scoped per-org.

## Observability

- The API has request-ID-tagged logs and a health endpoint at `/healthz`.
- The worker logs each job's outcome (done/failed) as it processes the queue.
