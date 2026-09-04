# Security Policy

Clevis handles GitHub App credentials, installation tokens, and org-level security data, so we
take vulnerability reports seriously and will respond promptly.

## Supported versions

Clevis is self-hosted and shipped as a rolling `main` branch rather than versioned releases.
Only the latest commit on `main` is supported with security fixes — if you're running an older
checkout, update before reporting an issue that may already be fixed.

## Reporting a vulnerability

**Please do not open a public GitHub issue for security vulnerabilities.**

Report privately via [GitHub Security Advisories](https://github.com/nazarli-shabnam/clevis/security/advisories/new)
("Report a vulnerability" under the Security tab). This reaches maintainers directly without
disclosing the issue publicly, and lets us collaborate with you on a fix and coordinate a
disclosure timeline through the same thread.

Please include:

- A description of the vulnerability and its impact.
- Steps to reproduce (a minimal repro is very helpful).
- The affected component (`apps/api`, `apps/worker`, `apps/ui`, or `packages/checks`) and commit
  SHA / branch you tested against.

We'll acknowledge new reports within 5 business days, and aim to provide a fix or mitigation
timeline within 14 days of confirming the issue. Once a fix is available, we'll coordinate with
you on disclosure — crediting reporters who want credit.

## Scope

In scope:

- `apps/api`, `apps/worker`, `apps/ui`, and `packages/checks` in this repository.
- Authentication and session handling (password + GitHub OAuth), RBAC (`require_auth`,
  `require_workspace_admin`, `require_org_role`), and GitHub App token handling.
- Token/credential encryption (`checks.crypto` and the API/worker `_crypto.py` wrappers),
  webhook signature verification, and audit logging.
- Multi-tenant isolation issues (e.g. one org/tenant reading or acting on another's data).

Out of scope:

- Vulnerabilities that require an attacker to already have workspace-admin or org-admin access
  they weren't supposed to have (report those as the underlying privilege-escalation bug
  instead).
- Denial-of-service reports based purely on volume (e.g. flooding an endpoint with requests).
- Issues in third-party dependencies — please report those upstream (and feel free to let us
  know too, so we can track the update).
- Findings from automated scanners without a demonstrated, concrete impact.

## Known architectural caveats

A few things are already tracked and intentionally not "surprise" reports:

- By default the API connects to Postgres as the bootstrap superuser (`DB_USER`), which bypasses
  Row-Level Security outright — RLS-based tenant isolation is only actually enforced once
  `API_DB_PASSWORD` is configured per [`docs/self-hosting.md`](docs/self-hosting.md). We still
  welcome reports of tenant-isolation bugs at the *application* layer (missing
  `require_org_role` checks, etc.).
- The legacy personal-access-token path (`saved_tokens`) stores a Fernet-encrypted token per org;
  prefer a GitHub App installation, which uses short-lived, per-org scoped tokens instead.

## Preferred languages

English.
