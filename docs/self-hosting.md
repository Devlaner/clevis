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

2. Fill in `.env`. Six vars are hard-required (the app fails to start without them) — `DB_USER`, `DB_PASSWORD`, `DB_NAME`, `JOB_SECRET_KEY` (`openssl rand -hex 32`), `AUTH_SECRET` (`openssl rand -hex 32`), `NEXT_PUBLIC_API_BASE`. Everything else in `.env.example` has a safe default and only needs overriding per environment (e.g. `CORS_ORIGINS` for your real UI domain, `GITHUB_API_BASE` for GitHub Enterprise).

3. Register a GitHub App on github.com (**Settings → Developer settings → GitHub Apps → New GitHub App**) so users can connect their orgs after the instance is running. The App form asks for three separate URLs — each one is where GitHub sends the browser back to at a different point in the flow, so don't reuse one URL for another's purpose:
   - **Homepage URL** — your deployed UI URL. Shown on the App's public listing page; not used in any redirect.
   - **Callback URL** — `<NEXT_PUBLIC_API_BASE>/auth/github/callback`. GitHub redirects here after a user clicks "Sign in with GitHub" and approves the login. Must match this exact path, or sign-in fails.
   - **Setup URL** (under "Post installation") — `<NEXT_PUBLIC_UI_BASE>/settings/github-callback`, with **"Redirect on update"** also checked. GitHub redirects here after a user clicks "Install GitHub App" on the Settings page and finishes installing the App on an org. Setting this URL is what lets Clevis learn the installation happened: on that redirect, the UI records a new `github_installations` row for the org. If the Setup URL field is left blank, GitHub instead redirects to the Homepage URL above — Clevis never sees that callback, so the org never gets connected, and the app silently behaves as if nothing was installed at all (no error, just a permanently empty org).
   - **Webhook** — optional, but recommended: set the webhook URL to `<NEXT_PUBLIC_API_BASE>/webhooks/github`, generate a webhook secret in the same form, and copy that secret into `.env` as `GITHUB_APP_WEBHOOK_SECRET`. Without a webhook configured, Clevis has no way to find out when a user uninstalls the App on GitHub's side, so that org's `github_installations` row is never cleaned up — it just goes stale in the database.
   - Grant read access to the repository/organization data the security checks need (contents, metadata, administration, members).
   - **Private key** — this is a separate secret from the webhook secret above; GitHub doesn't ask for it on the creation form. After the App is created, open its settings page, scroll to the **"Private keys"** section (near the bottom, under "General"), and click **"Generate a private key"**. This downloads a `<something>.pem` file — open it and copy the *entire* contents, including the `-----BEGIN RSA PRIVATE KEY-----` / `-----END RSA PRIVATE KEY-----` lines, into `.env` as `GITHUB_APP_PRIVATE_KEY`.
   - Copy the remaining values into `.env`: `GITHUB_APP_ID`, `GITHUB_APP_CLIENT_ID`, `GITHUB_APP_CLIENT_SECRET` (all shown on the same settings page, near the top). Also set `NEXT_PUBLIC_GITHUB_APP_SLUG` to the App's slug (found in its public page URL) — the UI uses this slug to build the "Install GitHub App" button's link.

   **If your GitHub App was registered before the Setup URL field above was documented here:** open the App's settings on github.com, go to the "Post installation" section, and add the Setup URL now. Orgs that already completed installation don't need to reinstall — only new installs and updates will use the new callback page going forward.

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
   docker compose exec db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "GRANT SELECT, UPDATE ON jobs TO clevis_worker; GRANT SELECT ON app_config TO clevis_worker;"'
   ```

   Don't rely on re-running `alembic upgrade head` for step 2 — if migration `0020` already applied (as a no-op, since the role didn't exist yet), Alembic considers it done and won't re-run it. Restart the `worker` container afterwards to pick up the new credential.

6. Start the stack:

   ```bash
   docker compose up --build -d
   ```

   Or, on a tagged release, pull the pre-built images instead of building from source:

   ```
   ghcr.io/<owner>/clevis-api
   ghcr.io/<owner>/clevis-worker
   ghcr.io/<owner>/clevis-ui
   ```

7. Verify it's up:

   ```bash
   curl http://localhost:8080/healthz   # -> {"status": "ok"}
   ```

   Then open the UI and continue with the [Get started](../README.md#get-started) flow in the README.

## Security notes

- Sign-in uses a JWT held in an httpOnly session cookie, not a trusted request header — set `session_cookie_secure=false` only for local HTTP dev, never in production.
- Restrict API ingress behind your reverse proxy/SSO; the base `docker-compose.yml` deliberately publishes no host ports (Traefik-only) for this reason.
- Prefer GitHub App auth over the legacy personal-access-token path for anything beyond local testing — tokens are Fernet-encrypted at rest either way, but App installation tokens are short-lived and scoped per-org.

## Observability

- The API has request-ID-tagged logs and a health endpoint at `/healthz`.
- The worker logs each job's outcome (done/failed) as it processes the queue.
