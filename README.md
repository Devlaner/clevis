<div align="center">

# Clevis

### Analytics and security insight for GitHub repositories and organizations.

<br/>

[![CI](https://img.shields.io/github/actions/workflow/status/nazarli-shabnam/clevis/ci.yml?style=for-the-badge&logo=githubactions&logoColor=white&label=CI)](https://github.com/nazarli-shabnam/clevis/actions/workflows/ci.yml)
[![CodeQL](https://img.shields.io/github/actions/workflow/status/nazarli-shabnam/clevis/codeql.yml?style=for-the-badge&logo=github&logoColor=white&label=CodeQL)](https://github.com/nazarli-shabnam/clevis/actions/workflows/codeql.yml)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-a855f7?style=for-the-badge)](LICENSE)
[![Self-host](https://img.shields.io/badge/Self--host-Docker_Compose-2496ED?style=for-the-badge&logo=docker&logoColor=white)](#quickstart)

<br/>

**Clevis turns the GitHub API into a dashboard.** Track repository activity, audit organization security posture, and run privileged maintenance jobs — from a single, dense, self-hosted control panel.

</div>

---

## What it does

| Area | Description |
|------|-------------|
| **Overview** | At-a-glance stats across your repositories and organization. |
| **Activity** | Repository and org event streams pulled from the GitHub API. |
| **Repositories** | Browse and inspect repositories with their key signals in one place. |
| **Health & Security** | A computed **security score** backed by automated checks — **MFA enforcement**, **branch protection**, and **secret scanning** — each with pass/fail status and remediation guidance. |
| **Collaborators** | See who has access across the surface you manage. |
| **Automation** | Run privileged maintenance jobs (e.g. clearing GitHub Actions caches) with **dry-run** support and a full **audit trail**. |

Every privileged action is recorded in an immutable audit log, and long-running work is dispatched to a background worker through a Postgres-backed job queue.

---

## Architecture

Clevis is three independently deployable services around one shared check library:

| Component | Stack | Responsibility |
|-----------|-------|----------------|
| **`apps/api`** | FastAPI · SQLAlchemy 2 · Alembic | REST backend — auth, analytics, RBAC, and job enqueueing. |
| **`apps/worker`** | Python · psycopg3 | Polls the job queue (`SELECT … FOR UPDATE SKIP LOCKED`) and executes GitHub API tasks. Scales to multiple replicas safely. |
| **`apps/ui`** | Next.js 15 · React 19 · TanStack Query | The dashboard — dense, dark, keyboard-friendly. |
| **`packages/checks`** | `clevis-checks` (Python) | The security-check engine (MFA, branch protection, secret scanning) with built-in GitHub pagination. |

**Security model:** Access is enforced per route with JWT auth dependencies (`require_auth`, `require_workspace_admin`) and org-scoped roles (`require_org_role("member"|"admin")`). GitHub credentials may be stored as Fernet-encrypted rows in `saved_tokens` (legacy PAT path); job payloads are also Fernet-encrypted at enqueue time and decrypted only when the worker processes them. Prefer a connected GitHub App installation so the API can mint short-lived installation tokens instead. Every request carries a propagated `X-Request-ID` for traceability.

---

## Built with

<div align="center">

![Python](https://img.shields.io/badge/Python-3776AB?style=for-the-badge&logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?style=for-the-badge&logo=fastapi&logoColor=white)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-4169E1?style=for-the-badge&logo=postgresql&logoColor=white)
![SQLAlchemy](https://img.shields.io/badge/SQLAlchemy-D71F00?style=for-the-badge&logo=sqlalchemy&logoColor=white)
<br/>
![Next.js](https://img.shields.io/badge/Next.js_15-000000?style=for-the-badge&logo=next.js&logoColor=white)
![React](https://img.shields.io/badge/React_19-20232A?style=for-the-badge&logo=react&logoColor=61DAFB)
![TypeScript](https://img.shields.io/badge/TypeScript-3178C6?style=for-the-badge&logo=typescript&logoColor=white)
![Tailwind CSS](https://img.shields.io/badge/Tailwind_v4-38B2AC?style=for-the-badge&logo=tailwind-css&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-2496ED?style=for-the-badge&logo=docker&logoColor=white)

</div>

---

## Quickstart

**Prerequisites:** Docker + Docker Compose.

```bash
git clone https://github.com/nazarli-shabnam/clevis
cd clevis
cp .env.example .env        # fill in every variable — none have defaults
docker compose up --build -d
```

Then open the UI at **`http://localhost:3000`**.

Full env var reference, GitHub App registration, and production hardening notes live in [`docs/self-hosting.md`](docs/self-hosting.md). Local dev without Docker is covered in [`AGENTS.md`](AGENTS.md#running-locally).

---

## Get started

Once the instance is running, here's how to go from a fresh deploy to a connected GitHub org:

1. **Create the first account.** The first visit lands on `/setup`, which creates the initial admin. After that, sign in with a password or the "Sign in with GitHub" button.
2. **Connect a GitHub org or account.** Go to **Settings → Connected orgs** and click **Install GitHub App** — this sends you to GitHub to authorize Clevis for that org/account, which is what grants read access to its repos. GitHub redirects you back into the app once you approve it, and the org/account shows up under "Connected orgs" a few seconds later. (The App itself is a one-time setup step for whoever deployed the instance — see [`docs/self-hosting.md`](docs/self-hosting.md) if that hasn't been done yet.) If you install into a brand-new org and the connection doesn't complete, sign out and back in with **"Sign in with GitHub"** once (this is what verifies your admin access on that org) and retry the install.

That's it — Overview, Activity, Repositories, Collaborators, Health & Security, and Cache all work off the connected org from step 2. A legacy personal access token (**Settings → Personal access tokens (legacy)**) is only ever needed as a manual fallback for an org that has no GitHub App installation — it is not a required step.

**A pasted PAT alone does not connect a new org to Clevis.** It only supplies GitHub-side credentials for an org that's already connected via step 2 above; Repositories, Activity, Releases, Automation, and Collaborators all require that connection first (only Overview's `/me/analytics/overview` endpoint has no such requirement). If you paste a token for an org Clevis has never seen, you'll get a clear "isn't connected to Clevis yet" error rather than being able to browse that org's data.

---

## Contributing

Contributions are welcome — see [`CONTRIBUTING.md`](CONTRIBUTING.md) for dev setup, CI checks, and commit conventions, and [`DESIGN.md`](DESIGN.md) for UI design language.

Found a security issue? Please follow the [security policy](SECURITY.md) rather than opening a public issue.

## License

Released under the [GNU General Public License v3.0](LICENSE).
