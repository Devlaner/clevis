"""Security compliance matrix and secret-scanning alerts (docs/plan.md Phase 16).

The org-wide `analytics.overview` scan already computes org-level pass/fail check
results (see `checks.runner.run_all_checks`) -- this router instead breaks the same
dimensions down per-repo, which is what an admin needs to act on a specific finding.
It re-derives each dimension directly from the GitHub API per repo rather than reusing
`checks.github_checks`, since those check classes are written to aggregate across all
repos into a single org-wide pass/fail, not to return a per-repo row.

Personal-scoped (`/me/...`), matching `analytics.py`'s `/me/analytics/overview` --
the Security page scans an arbitrary owner by name, not necessarily a workspace Org
the caller has an `OrgMembership` row for, so there's no `require_org_role` path
gating this route directly. Token resolution still prefers an org-scoped installation
over a personal one when `owner` does match an Org the caller is a member of, via
`resolve_owner_token` -- see its docstring for why that's still membership-gated.
"""

from concurrent.futures import ThreadPoolExecutor

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.orm import Session

from src.core.auth import UserOut, require_auth
from src.core.db import SecurityAlert, get_db
from src.core.rbac import set_tenant_session_context
from src.repositories import installation_repo, org_repo, tenant_repo
from src.schemas.security import (
    MatrixSummary,
    RepoSecurityRow,
    SecretAlert,
    SecretScanningResponse,
    SecurityMatrixResponse,
    VulnCounts,
)
from src.services.github_client import GitHubClient, github_error as _github_error
from src.services.token_resolution import NoGitHubTokenAvailable, resolve_owner_token

router = APIRouter()

# Each repo costs up to 3 additional GitHub calls (branch, dependabot, code-scanning)
# on top of the initial repo list, so the cap here is tighter than the single-call
# aggregate helpers in analytics.py.
_MAX_REPOS_FOR_MATRIX = 20


def _branch_protection_status(exc: httpx.HTTPStatusError) -> str:
    # Mirrors packages/checks/src/checks/github_checks.py's _branch_protection_status:
    # a 404 is a real negative answer ("unprotected"), not an error; 403/429 mean the
    # token can't see the answer at all, which must not be scored as a compliance fail.
    code = exc.response.status_code
    if code == 404:
        return "unprotected"
    if code in (403, 429):
        return "unknown"
    return "unprotected"


def _security_connected_tenant(db: Session, user_id: int, owner: str) -> int | None:
    """Mirrors analytics.py's _cockpit_connected_tenant exactly -- same personal-endpoint
    membership check (this router is also require_auth-only, no OrgContext), same
    installation_id-presence gate, same RLS session-context setup. Duplicated rather than
    imported: routers don't import each other's private helpers in this codebase (see
    repos.py's _repo_org_connected for the same precedent)."""
    org = org_repo.get_by_login_ci(db, owner)
    if org is None:
        return None
    org = org_repo.ensure_tenant_linked(db, org)
    if tenant_repo.get_membership(db, org.tenant_id, user_id) is None:
        return None
    installation = installation_repo.get_for_org(db, org_id=org.id, account_login=owner)
    if installation is None or installation.installation_id is None:
        return None
    set_tenant_session_context(db, org.tenant_id, user_id)
    return org.tenant_id


def _open_alerts_by_repo(db: Session, tenant_id: int, repo_full_names: list[str]) -> dict[str, list]:
    """One query for every scanned repo's open dependabot/code_scanning security_alerts
    rows, not one query per repo -- also sidesteps ThreadPoolExecutor entirely: _repo_row
    below runs concurrently across repos (existing pattern), and a SQLAlchemy Session
    isn't safe to share across threads, so this pre-fetches everything up front on the
    request thread and hands each worker a plain dict slice instead of `db` itself."""
    if not repo_full_names:
        return {}
    rows = (
        db.query(SecurityAlert.repo, SecurityAlert.kind, SecurityAlert.severity)
        .filter(
            SecurityAlert.tenant_id == tenant_id,
            SecurityAlert.repo.in_(repo_full_names),
            SecurityAlert.kind.in_(("dependabot", "code_scanning")),
            SecurityAlert.state == "open",
        )
        .all()
    )
    by_repo: dict[str, list] = {}
    for row in rows:
        by_repo.setdefault(row.repo, []).append(row)
    return by_repo


def _dependabot_and_code_scanning_from_aggregate(alert_rows: list) -> dict:
    """dependabot/code_scanning dimensions from security_alerts (post-S6 PR 3) instead of
    two live GitHub calls per repo. dependabot_enabled is an approximation here: security_alerts
    only has rows for alerts that actually occurred, so a repo with Dependabot enabled but
    zero alerts ever looks identical to one with it disabled -- both read as
    dependabot_enabled=False, critical/high=0. This under-reports the "enabled" badge for a
    clean repo but never affects the score (an all-zero count scores as compliant either
    way, matching the live path's own 404-disabled handling)."""
    dependabot_rows = [r for r in alert_rows if r.kind == "dependabot"]
    return {
        "dependabot_enabled": len(dependabot_rows) > 0,
        "critical_count": sum(1 for r in dependabot_rows if r.severity == "critical"),
        "high_count": sum(1 for r in dependabot_rows if r.severity == "high"),
        "code_scanning_clear": not any(r.kind == "code_scanning" for r in alert_rows),
    }


def _repo_row(client: GitHubClient, owner: str, repo: dict, alerts_by_repo: dict[str, list] | None = None) -> RepoSecurityRow:
    name = repo["name"]
    branch = repo.get("default_branch")
    unknown: list[str] = []

    branch_protection = False
    force_push_allowed = False
    try:
        details = client.request("GET", f"/repos/{owner}/{name}/branches/{branch}")
        if isinstance(details, dict):
            branch_protection = bool(details.get("protected"))
            protection = details.get("protection") or {}
            force_push_allowed = bool((protection.get("allow_force_pushes") or {}).get("enabled"))
    except httpx.HTTPStatusError as exc:
        # force_push_allowed comes from the same branch-details response, so an
        # unknown branch_protection answer means force_push is equally unknown --
        # they must not resolve to opposite compliance verdicts from one failed call.
        if _branch_protection_status(exc) == "unknown":
            unknown.extend(["branch_protection", "force_push"])
    except httpx.RequestError:
        # A transient network error is exactly as unknowable as a 403/429 -- neither
        # is a real "unprotected" answer from GitHub (see PR history: 10b10e9, 027d30b).
        unknown.extend(["branch_protection", "force_push"])

    secret_scanning = (
        (repo.get("security_and_analysis") or {}).get("secret_scanning") or {}
    ).get("status") == "enabled"

    alerts_source = "github"
    if alerts_by_repo is not None:
        alerts_source = "aggregate"
        aggregate = _dependabot_and_code_scanning_from_aggregate(alerts_by_repo.get(f"{owner}/{name}", []))
        dependabot_enabled = aggregate["dependabot_enabled"]
        critical_count = aggregate["critical_count"]
        high_count = aggregate["high_count"]
        code_scanning_clear = aggregate["code_scanning_clear"]
    else:
        dependabot_enabled = False
        critical_count = 0
        high_count = 0
        try:
            alerts = client.request("GET", f"/repos/{owner}/{name}/dependabot/alerts", params={"state": "open"})
            dependabot_enabled = True
            if isinstance(alerts, list):
                for alert in alerts:
                    severity = (alert.get("security_advisory") or {}).get("severity")
                    if severity == "critical":
                        critical_count += 1
                    elif severity == "high":
                        high_count += 1
        except httpx.HTTPStatusError as exc:
            # 404 means Dependabot alerts are genuinely disabled for this repo -- a real
            # "no alerts" answer. Any other status (403 missing security-events scope,
            # 429, ...) means the alert count is unknown, not zero, so it must not
            # silently score as "no critical/high alerts" -- see the identical fix in
            # DependabotAlertsCheck (packages/checks/src/checks/github_checks.py, 3184c76).
            if exc.response.status_code != 404:
                unknown.append("dependabot")
        except httpx.RequestError:
            unknown.append("dependabot")

        code_scanning_clear = True
        try:
            cs_alerts = client.request("GET", f"/repos/{owner}/{name}/code-scanning/alerts", params={"state": "open"})
            if isinstance(cs_alerts, list):
                code_scanning_clear = len(cs_alerts) == 0
        except httpx.HTTPStatusError as exc:
            # Same 404-vs-other distinction as Dependabot above: 404 is a genuine
            # "disabled, so no alerts" answer; anything else means no visibility.
            if exc.response.status_code != 404:
                unknown.append("code_scanning")
        except httpx.RequestError:
            unknown.append("code_scanning")

    dimensions = {
        "branch_protection": branch_protection,
        "secret_scanning": secret_scanning,
        "dependabot": critical_count == 0 and high_count == 0,
        "code_scanning": code_scanning_clear,
        "force_push": not force_push_allowed,
    }
    evaluable = {k: v for k, v in dimensions.items() if k not in unknown}
    score = round(100 * sum(evaluable.values()) / len(evaluable)) if evaluable else 0

    return RepoSecurityRow(
        repo=name,
        branch_protection=branch_protection,
        secret_scanning=secret_scanning,
        dependabot_enabled=dependabot_enabled,
        dependabot_critical_count=critical_count,
        dependabot_high_count=high_count,
        code_scanning=code_scanning_clear,
        force_push_allowed=force_push_allowed,
        score=score,
        unknown_dimensions=unknown,
        alerts_source=alerts_source,
    )


def _build_matrix(owner: str, token: str, db: Session | None = None, tenant_id: int | None = None) -> SecurityMatrixResponse:
    client = GitHubClient(token)
    repos = client.request_paginated(f"/orgs/{owner}/repos", params={"type": "all", "sort": "pushed"})
    scanned = repos[:_MAX_REPOS_FOR_MATRIX]

    alerts_by_repo = None
    if db is not None and tenant_id is not None:
        alerts_by_repo = _open_alerts_by_repo(db, tenant_id, [f"{owner}/{r['name']}" for r in scanned])

    with ThreadPoolExecutor(max_workers=10) as pool:
        rows = list(pool.map(lambda r: _repo_row(client, owner, r, alerts_by_repo), scanned))

    vuln = VulnCounts(
        critical=sum(r.dependabot_critical_count for r in rows),
        high=sum(r.dependabot_high_count for r in rows),
        medium=0,
        low=0,
    )
    summary = MatrixSummary(
        fully_compliant_count=sum(1 for r in rows if r.score == 100),
        critical_risk_count=sum(1 for r in rows if r.dependabot_critical_count > 0),
        secret_hits_count=sum(1 for r in rows if not r.secret_scanning),
        vuln_by_severity=vuln,
    )
    return SecurityMatrixResponse(owner=owner, repos=rows, summary=summary)


@router.get("/me/analytics/security-matrix/{owner}", response_model=SecurityMatrixResponse)
def personal_security_matrix(
    owner: str,
    user: UserOut = Depends(require_auth),
    db: Session = Depends(get_db),
    x_github_token: str | None = Header(default=None),
):
    try:
        token = resolve_owner_token(db, user_id=user.id, owner=owner, client_token=x_github_token)
    except NoGitHubTokenAvailable as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    tenant_id = _security_connected_tenant(db, user.id, owner)
    try:
        return _build_matrix(owner, token, db=db, tenant_id=tenant_id)
    except (httpx.HTTPStatusError, httpx.RequestError) as exc:
        raise _github_error(exc) from exc


def _secret_scanning_alerts_from_aggregate(db: Session, tenant_id: int, owner: str, repo: str) -> list[SecretAlert]:
    """security_alerts (post-S6 PR 3) instead of a live GitHub call. No `url` field is
    stored (only the live GitHub API response carries html_url) -- left empty, same as
    an alert this table has no row for; the UI only uses it as an outbound link, not for
    anything that gates alert-handling logic."""
    rows = (
        db.query(SecurityAlert)
        .filter(
            SecurityAlert.tenant_id == tenant_id,
            SecurityAlert.repo == f"{owner}/{repo}",
            SecurityAlert.kind == "secret_scanning",
        )
        .all()
    )
    return [
        SecretAlert(
            number=row.number,
            state=row.state,
            secret_type=row.details.get("secret_type", ""),
            secret_type_display=row.details.get("secret_type_display_name", row.details.get("secret_type", "")),
            resolved_reason=row.details.get("resolution"),
            created_at=row.created_at,
            resolved_at=row.updated_at if row.state != "open" else None,
            repo=f"{owner}/{repo}",
            url="",
        )
        for row in rows
    ]


@router.get("/me/repos/{owner}/{repo}/secret-scanning", response_model=SecretScanningResponse)
def personal_secret_scanning(
    owner: str,
    repo: str,
    user: UserOut = Depends(require_auth),
    db: Session = Depends(get_db),
    x_github_token: str | None = Header(default=None),
):
    try:
        token = resolve_owner_token(db, user_id=user.id, owner=owner, client_token=x_github_token)
    except NoGitHubTokenAvailable as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    tenant_id = _security_connected_tenant(db, user.id, owner)
    if tenant_id is not None:
        alerts = _secret_scanning_alerts_from_aggregate(db, tenant_id, owner, repo)
        return SecretScanningResponse(repository=f"{owner}/{repo}", alerts=alerts, source="aggregate")

    client = GitHubClient(token)
    try:
        raw = client.request("GET", f"/repos/{owner}/{repo}/secret-scanning/alerts", params={"per_page": 50})
    except (httpx.HTTPStatusError, httpx.RequestError) as exc:
        raise _github_error(exc) from exc

    alerts = [
        SecretAlert(
            number=a["number"],
            state=a.get("state", "open"),
            secret_type=a.get("secret_type", ""),
            # GitHub's actual field name is secret_type_display_name, not secret_type_display.
            secret_type_display=a.get("secret_type_display_name", a.get("secret_type", "")),
            resolved_reason=a.get("resolution"),
            created_at=a["created_at"],
            resolved_at=a.get("resolved_at"),
            repo=f"{owner}/{repo}",
            url=a.get("html_url", ""),
        )
        for a in raw
        if isinstance(a, dict) and "number" in a and "created_at" in a
    ]
    return SecretScanningResponse(repository=f"{owner}/{repo}", alerts=alerts, source="github")
