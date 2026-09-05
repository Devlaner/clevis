import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import anyio
import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from sqlalchemy import func, text
from sqlalchemy.orm import Session

from src.core.app_config import get_config
from src.core.auth import UserOut, require_auth
from src.core.db import RepoEventDailyCount, get_db
from src.core.rbac import OrgContext, assert_owner_matches_org, require_org_role, set_tenant_session_context
from src.repositories import installation_repo, job_repo, org_repo, scan_results_repo, tenant_repo
from src.routers.github import _cached_events, _fetch_events_from_repo_events
from src.schemas.analytics import (
    ActionsUsageResponse,
    AnalyticsInput,
    AnalyticsResponse,
    AtRiskRepo,
    CockpitResponse,
    IssueSummary,
    MilestoneSummary,
    MyIssueListResponse,
    MyPrListResponse,
    MyViewResponse,
    OrgEventSummary,
    PRSummary,
    PrCycleTimeWeek,
    PrWeekBucket,
    RunSummaryLite,
    ScanExportResponse,
    ScanHistoryEntry,
)
from src.services.analytics_service import get_account_type, get_overview
from src.services.github_client import GitHubClient, github_error as _github_error
from src.services.token_resolution import NoGitHubTokenAvailable, resolve_org_token, resolve_owner_token

logger = logging.getLogger(__name__)

router = APIRouter()

# Bounds the per-repo fan-out in _safe_commit_activity_4w / _safe_total_cache_bytes --
# each additional repo costs one more GitHub call, so large orgs are capped.
_MAX_REPOS_FOR_AGGREGATES = 30
_CACHE_JOB_TYPE = "github.clear_actions_cache"


async def _run_overview(owner: str, token: str) -> AnalyticsResponse:
    try:
        return await anyio.to_thread.run_sync(lambda: get_overview(owner=owner, token=token))
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=400, detail=f"GitHub API error: {exc.response.status_code}")
    except httpx.RequestError:
        raise HTTPException(status_code=503, detail="GitHub API unreachable")
    except Exception:
        logger.exception("analytics_overview failed")
        raise HTTPException(status_code=500, detail="Internal error")


async def _get_account_type(owner: str, token: str) -> str:
    try:
        return await anyio.to_thread.run_sync(lambda: get_account_type(owner=owner, token=token))
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=400, detail=f"GitHub API error: {exc.response.status_code}")
    except httpx.RequestError:
        raise HTTPException(status_code=503, detail="GitHub API unreachable")


def _persist_scan(db: Session, result: dict, tenant_id: int | None, scanned_by_user_id: int | None = None) -> None:
    scan_results_repo.insert(
        db,
        owner=result["owner"],
        score=result["score"],
        total_checks=result["total_checks"],
        failed_checks=result["failed_checks"],
        checks=result["checks"],
        tenant_id=tenant_id,
        scanned_by_user_id=scanned_by_user_id,
    )


def _user_history_scope(db: Session, user: UserOut, owner: str) -> str | None:
    """How much of `owner`'s scan history this user may read. Scan history isn't
    gated by GitHub-side authorization the way a live scan is -- it's a local DB
    read, so it needs its own check. Returns:

    - ``"all"``  -- the user is a member of the matching workspace Org, or has a
      personal GitHub App installation for that account login: they see every
      scan of that owner.
    - ``"own"``  -- the user's only claim is a personal (BYO-PAT) scan they ran
      themselves against a login with no workspace Org/membership: they see only
      their own scans (`scanned_by_user_id`).
    - ``None``   -- no access.
    """
    org = org_repo.get_by_login(db, owner)
    if org is not None:
        org = org_repo.ensure_tenant_linked(db, org)
        if tenant_repo.get_membership(db, org.tenant_id, user.id) is not None:
            return "all"
    if installation_repo.get_for_user(db, owner_user_id=user.id, account_login=owner) is not None:
        return "all"
    if scan_results_repo.exists_for_user(db, owner=owner, user_id=user.id):
        return "own"
    return None


def _user_can_read_history(db: Session, user: UserOut, owner: str) -> bool:
    return _user_history_scope(db, user, owner) is not None


@router.post("/orgs/{org_login}/analytics/overview", response_model=AnalyticsResponse)
async def org_analytics_overview(
    payload: AnalyticsInput,
    ctx: OrgContext = Depends(require_org_role(min_role="member")),
    db: Session = Depends(get_db),
):
    assert_owner_matches_org(payload.owner, ctx)
    client_token = payload.token.get_secret_value() if payload.token else None
    try:
        token = await anyio.to_thread.run_sync(
            lambda: resolve_org_token(db, org_id=ctx.org.id, account_login=payload.owner, client_token=client_token)
        )
    except NoGitHubTokenAvailable as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    result = await _run_overview(payload.owner, token)
    _persist_scan(db, result, tenant_id=ctx.org.tenant_id)
    return result


@router.post("/me/analytics/overview", response_model=AnalyticsResponse)
async def personal_analytics_overview(
    payload: AnalyticsInput,
    user: UserOut = Depends(require_auth),
    db: Session = Depends(get_db),
):
    client_token = payload.token.get_secret_value() if payload.token else None
    try:
        token = await anyio.to_thread.run_sync(
            lambda: resolve_owner_token(db, user_id=user.id, owner=payload.owner, client_token=client_token)
        )
    except NoGitHubTokenAvailable as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    account_type = await _get_account_type(payload.owner, token)
    if account_type == "User":
        raise HTTPException(
            status_code=422,
            detail="Personal GitHub accounts aren't supported for security scanning yet. Connect a GitHub organization instead.",
        )
    result = await _run_overview(payload.owner, token)
    # payload.owner here can be any GitHub account the user has a token for (bring-your-own-
    # token path), not necessarily a Clevis org -- the scan row is associated with the
    # scanning user's own personal tenant, consistent with the existing scanned_by_user_id-
    # based access-gating design (see ScanResult.tenant_id/scanned_by_user_id in db.py).
    personal_tenant = tenant_repo.ensure_personal_tenant(db, user.id)
    _persist_scan(db, result, tenant_id=personal_tenant.id, scanned_by_user_id=user.id)
    return result


@router.get("/orgs/{org_login}/analytics/history", response_model=list[ScanHistoryEntry])
def org_analytics_history(
    ctx: OrgContext = Depends(require_org_role(min_role="member")),
    db: Session = Depends(get_db),
):
    return scan_results_repo.list_recent(db, owner=ctx.org.github_login, limit=30)


def _billing_num(value: object) -> float:
    """Coerce a GitHub billing quantity to a float, defaulting a missing / non-numeric
    field to 0.0 rather than raising (the usage API's numeric fields are documented as
    required, but we don't want a shape drift to 500 the whole Overview)."""
    return float(value) if isinstance(value, (int, float)) else 0.0


@router.get("/orgs/{org_login}/usage/actions", response_model=ActionsUsageResponse)
def org_actions_usage(
    response: Response,
    ctx: OrgContext = Depends(require_org_role(min_role="admin")),
    db: Session = Depends(get_db),
    x_github_token: str | None = Header(default=None),
):
    """GitHub Actions minutes used this billing month for the org (issue #294).

    **Needs a GitHub App permission Clevis does not request by default.** Reading
    ``GET /organizations/{org}/settings/billing/usage/summary`` requires the org
    **Administration** permission (read); billing was an explicitly deferred roadmap
    area. Read-only. When the App lacks it GitHub returns 403, surfaced here as a 400
    with a clear hint so the UI can hide the card rather than error the page.

    This replaces the retired ``/orgs/{org}/settings/billing/actions`` endpoint
    (GitHub shut it down on 2025-09-26). The response is billing data, so it's served
    ``Cache-Control: no-store`` — it must never sit in a shared/browser cache where a
    later, lower-privilege session could read it (CWE-525). No migration.
    """
    try:
        token = resolve_org_token(
            db, org_id=ctx.org.id, account_login=ctx.org.github_login, client_token=x_github_token
        )
    except NoGitHubTokenAvailable as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    now = datetime.now(timezone.utc)
    try:
        data = GitHubClient(token).request(
            "GET",
            f"/organizations/{ctx.org.github_login}/settings/billing/usage/summary",
            params={"year": now.year, "month": now.month, "product": "actions"},
        )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 403:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Clevis's GitHub App can't read this org's Actions billing — grant it "
                    "the org 'Administration' (plan) permission. See docs/self-hosting.md."
                ),
            ) from exc
        raise _github_error(exc) from exc
    except httpx.RequestError as exc:
        raise _github_error(exc) from exc

    if not isinstance(data, dict) or not isinstance(data.get("usageItems"), list):
        raise HTTPException(status_code=502, detail="Unexpected response from GitHub billing API")

    total = included = paid = 0.0
    breakdown: dict[str, float] = {}
    for item in data["usageItems"]:
        if not isinstance(item, dict):
            raise HTTPException(status_code=502, detail="Unexpected response from GitHub billing API")
        # The summary is already product-filtered, but it still carries Actions
        # *storage* (unitType "GB") alongside minutes — count only the minutes.
        # GitHub's casing for unit types isn't contractually fixed, so match loosely.
        if str(item.get("unitType", "")).lower() != "minutes":
            continue
        gross = _billing_num(item.get("grossQuantity"))
        total += gross
        included += _billing_num(item.get("discountQuantity"))
        paid += _billing_num(item.get("netQuantity"))
        sku = str(item.get("sku") or "actions")
        breakdown[sku] = breakdown.get(sku, 0.0) + gross

    response.headers["Cache-Control"] = "no-store"
    return ActionsUsageResponse(
        total_minutes_used=total,
        included_minutes_used=included,
        paid_minutes_used=paid,
        minutes_used_breakdown=breakdown,
    )


@router.get("/me/analytics/history", response_model=list[ScanHistoryEntry])
def personal_analytics_history(
    owner: str,
    user: UserOut = Depends(require_auth),
    db: Session = Depends(get_db),
):
    if not _user_can_read_history(db, user, owner):
        raise HTTPException(status_code=403, detail="You don't have access to this owner's scan history")
    return scan_results_repo.list_recent(db, owner=owner, limit=30)


# ---------------------------------------------------------------------------
# Compliance export (issue #293) -- the full scan-history rows *with* each
# scan's per-check breakdown, over an optional [since, until] window, so an
# auditor can pull a reporting period. Same access gating as the history
# endpoints above; the CSV rendering itself is done client-side.
# ---------------------------------------------------------------------------

_EXPORT_MAX_ROWS = 5000


def _export_window(since: date | None, until: date | None) -> tuple[datetime | None, datetime | None]:
    if since is not None and until is not None and since > until:
        raise HTTPException(status_code=422, detail="`since` must not be after `until`")
    since_dt = datetime.combine(since, datetime.min.time(), tzinfo=timezone.utc) if since else None
    # `until` is an inclusive calendar day: widen it to the end of that day so a
    # scan run at 14:00 on the `until` date isn't silently dropped.
    until_dt = (
        datetime.combine(until, datetime.max.time(), tzinfo=timezone.utc) if until else None
    )
    return since_dt, until_dt


def _build_export_response(rows: list[dict], limit: int) -> ScanExportResponse:
    # list_for_export fetches limit+1 so a full page is distinguishable from a
    # truncated one -- a compliance export must never be silently partial.
    truncated = len(rows) > limit
    entries = rows[:limit]
    return ScanExportResponse(truncated=truncated, row_count=len(entries), entries=entries)


@router.get("/orgs/{org_login}/analytics/export", response_model=ScanExportResponse)
def org_analytics_export(
    since: date | None = None,
    until: date | None = None,
    limit: int = Query(_EXPORT_MAX_ROWS, ge=1, le=_EXPORT_MAX_ROWS),
    ctx: OrgContext = Depends(require_org_role(min_role="member")),
    db: Session = Depends(get_db),
):
    since_dt, until_dt = _export_window(since, until)
    rows = scan_results_repo.list_for_export(
        db, owner=ctx.org.github_login, since=since_dt, until=until_dt, limit=limit
    )
    return _build_export_response(rows, limit)


@router.get("/me/analytics/export", response_model=ScanExportResponse)
def personal_analytics_export(
    owner: str,
    since: date | None = None,
    until: date | None = None,
    limit: int = Query(_EXPORT_MAX_ROWS, ge=1, le=_EXPORT_MAX_ROWS),
    user: UserOut = Depends(require_auth),
    db: Session = Depends(get_db),
):
    scope = _user_history_scope(db, user, owner)
    if scope is None:
        raise HTTPException(status_code=403, detail="You don't have access to this owner's scan history")
    since_dt, until_dt = _export_window(since, until)
    rows = scan_results_repo.list_for_export(
        db,
        owner=owner,
        since=since_dt,
        until=until_dt,
        limit=limit,
        # "own" scope: the caller only ever ran a personal BYO-PAT scan of this
        # login -- don't hand them scans other users ran (or an org's own rows).
        scanned_by_user_id=user.id if scope == "own" else None,
    )
    return _build_export_response(rows, limit)


# ---------------------------------------------------------------------------
# Overview cockpit (docs/plan.md Phase 12) -- aggregates DB reads plus several
# independent GitHub calls into one response. Each GitHub-calling helper below
# is best-effort except _safe_list_repos: a degraded cockpit (missing PR data
# because search was rate-limited, say) is more useful to an org-health
# dashboard than a 500/503 for the whole page, but nothing here is computable
# without at least the repo list, so that one call is allowed to fail hard.
# ---------------------------------------------------------------------------


def _safe_list_repos(owner: str, token: str) -> list[dict]:
    client = GitHubClient(token)
    return client.request_paginated(f"/orgs/{owner}/repos", params={"type": "all", "sort": "pushed"})


def _safe_member_count(owner: str, token: str) -> tuple[int, bool]:
    """Returns (count, ok) -- ok=False means the call failed and count is a fallback 0, not a real
    zero. Callers must fold `not ok` into CockpitResponse.degraded rather than trusting the bare
    count, which previously looked identical to "this org genuinely has zero members." """
    try:
        client = GitHubClient(token)
        return len(client.request_paginated(f"/orgs/{owner}/members")), True
    except (httpx.HTTPStatusError, httpx.RequestError):
        return 0, False


def _cockpit_connected_tenant(db: Session, user_id: int, owner: str) -> int | None:
    # Unlike repos.py/github.py's org-scoped endpoints, the cockpit is a personal
    # endpoint (require_auth only, no OrgContext/require_org_role) -- `owner` is
    # whatever GitHub login the caller resolved a token for via resolve_owner_token,
    # not necessarily a Clevis org the caller is a member of (bring-your-own-token is
    # allowed here). Mirrors resolve_owner_token's own org-membership+role resolution
    # (token_resolution.py) deliberately: without this membership check, any caller
    # could read another org's repo_event_daily_counts/repo_events just by naming its
    # login, the exact class of bug resolve_owner_token's own docstring says it guards
    # against for token resolution -- "there exists a connected installation for this
    # login" alone is not authorization to read its aggregate data.
    org = org_repo.get_by_login_ci(db, owner)
    if org is None:
        return None
    org = org_repo.ensure_tenant_linked(db, org)
    if tenant_repo.get_membership(db, org.tenant_id, user_id) is None:
        return None
    installation = installation_repo.get_for_org(db, org_id=org.id, account_login=owner)
    if installation is None or installation.installation_id is None:
        return None
    # Sets RLS session context for this connection so the aggregate reads below (repo_events/
    # repo_event_daily_counts, migrations 0036/0037, both ENABLE ROW LEVEL SECURITY with a
    # strict tenant_id filter) are correctly tenant-scoped -- resolve_owner_token
    # itself doesn't set this (a separate, pre-existing gap in that shared helper, out of
    # scope here; see its own docstring), so this is the first point in the cockpit's
    # request handling where it's safe to do so, now that real membership is confirmed.
    set_tenant_session_context(db, org.tenant_id, user_id)
    return org.tenant_id


def _safe_recent_events(
    db: Session, owner: str, token: str, tenant_id: int | None
) -> tuple[list[OrgEventSummary], bool]:
    """Returns (events, ok) -- ok=False means the underlying fetch failed and events is a fallback
    [], not a real "no activity" answer."""
    try:
        if tenant_id is not None:
            events = _fetch_events_from_repo_events(db, owner, tenant_id, per_page=10).events
        else:
            events = _cached_events(owner, token, per_page=10).events
        return [OrgEventSummary(**e.model_dump()) for e in events[:5]], True
    except (httpx.HTTPStatusError, httpx.RequestError, HTTPException):
        return [], False


_ACTIVITY_STALE_HOURS_DEFAULT = 6


def _activity_stale_hours() -> int:
    # Mirrors gap_heal_sweep.py's own _read_stale_hours -- same config key, same clamp -- so
    # "stale" means the same thing here as it does to the sweep that's supposed to fix it.
    raw = get_config("gap_heal_stale_hours", str(_ACTIVITY_STALE_HOURS_DEFAULT))
    try:
        return max(1, min(168, int(raw)))
    except ValueError:
        return _ACTIVITY_STALE_HOURS_DEFAULT


def _recent_events_staleness(db: Session, tenant_id: int) -> bool:
    """True if this tenant's activity ingestion cursor is older than gap_heal_stale_hours (or has
    never synced at all) -- surfaced to the UI so a stalled pipeline shows old data labeled as old,
    instead of silently looking current. Tenant session context is already set by
    _cockpit_connected_tenant before this runs, so this read is correctly RLS-scoped."""
    row = db.execute(
        text("SELECT last_synced_at FROM activity_sync_cursors WHERE tenant_id = :tenant_id"),
        {"tenant_id": tenant_id},
    ).fetchone()
    if row is None or row[0] is None:
        return True
    last_synced_at = row[0]
    if last_synced_at.tzinfo is None:
        last_synced_at = last_synced_at.replace(tzinfo=timezone.utc)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=_activity_stale_hours())
    return last_synced_at < cutoff


def _cockpit_events_and_commit_activity(
    db: Session, owner: str, token: str, tenant_id: int | None, repo_names: list[str]
) -> tuple[list[OrgEventSummary], bool, bool, tuple[list[int], list[int], bool]]:
    # Bundled into one call, not two independent asyncio.gather entries, because both
    # branches below read `db` when tenant_id is set -- a SQLAlchemy Session isn't safe
    # to use concurrently from two threads at once, unlike every other helper in the
    # cockpit's gather, which only ever touches the GitHub token/client.
    recent_events, recent_events_ok = _safe_recent_events(db, owner, token, tenant_id)
    recent_events_stale = _recent_events_staleness(db, tenant_id) if tenant_id is not None else False
    if tenant_id is not None:
        commit_activity = (*_cockpit_commit_activity_from_aggregate(db, tenant_id), True)
    else:
        commit_activity = _safe_commit_activity_4w_and_heatmap_52w(owner, token, repo_names)
    return recent_events, not recent_events_ok, recent_events_stale, commit_activity


def _cockpit_commit_activity_from_aggregate(db: Session, tenant_id: int) -> tuple[list[int], list[int]]:
    """Same {4w, 52w} shape _safe_commit_activity_4w_and_heatmap_52w returns, built from
    repo_event_daily_counts' push-event counts summed across every repo in the tenant
    (the cockpit's commit_activity_4w/commit_heatmap_52w are org-wide, unlike repos.py's
    per-repo aggregate) instead of GitHub's actual per-repo commit counts -- an
    approximation for the same reason repos.py's aggregate is: a push can carry multiple
    commits. Callers must set CockpitResponse.commit_activity_source="aggregate"."""
    today = datetime.now(timezone.utc).date()
    current_week_start = today - timedelta(days=(today.weekday() + 1) % 7)
    oldest_week_start = current_week_start - timedelta(weeks=51)

    rows = (
        db.query(RepoEventDailyCount.day, func.sum(RepoEventDailyCount.count))
        .filter(
            RepoEventDailyCount.tenant_id == tenant_id,
            RepoEventDailyCount.event_type == "push",
            RepoEventDailyCount.day >= oldest_week_start,
        )
        .group_by(RepoEventDailyCount.day)
        .all()
    )
    counts_by_day = {day: int(count) for day, count in rows}

    totals_52w = [
        sum(counts_by_day.get(oldest_week_start + timedelta(weeks=i, days=d), 0) for d in range(7))
        for i in range(52)
    ]
    return totals_52w[-4:], totals_52w


def _week_start(weeks_ago: int) -> date:
    today = date.today()
    start_of_this_week = today - timedelta(days=today.weekday())
    return start_of_this_week - timedelta(weeks=weeks_ago)


def _search_count(client: GitHubClient, query: str) -> int:
    result = client.request("GET", "/search/issues", params={"q": query, "per_page": 1})
    return result.get("total_count", 0) if isinstance(result, dict) else 0


def _safe_open_pr_count(owner: str, token: str) -> tuple[int, bool]:
    try:
        client = GitHubClient(token)
        return _search_count(client, f"org:{owner} type:pr state:open"), True
    except (httpx.HTTPStatusError, httpx.RequestError):
        return 0, False


def _safe_pr_merge_rate_4w(owner: str, token: str) -> list[PrWeekBucket]:
    try:
        client = GitHubClient(token)
        week_starts = [_week_start(weeks_ago) for weeks_ago in range(3, -1, -1)]
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [
                (
                    start,
                    pool.submit(_search_count, client, f"org:{owner} type:pr created:{start}..{start + timedelta(days=7)}"),
                    pool.submit(_search_count, client, f"org:{owner} type:pr merged:{start}..{start + timedelta(days=7)}"),
                )
                for start in week_starts
            ]
            return [
                PrWeekBucket(week=start.isoformat(), opened=opened_f.result(), merged=merged_f.result())
                for start, opened_f, merged_f in futures
            ]
    except (httpx.HTTPStatusError, httpx.RequestError):
        return []


def _week_total(week: dict) -> int:
    """Raises AttributeError (non-dict week) or TypeError (not a real non-negative commit count)
    for the caller to treat as a per-repo failure -- a malformed nested record from GitHub must
    not silently coerce into 0, which would look identical to a real zero-commit week. A commit
    count is always a non-negative plain int on GitHub's side; explicitly excludes bool (`bool`
    is a subclass of `int` in Python, so `isinstance(True, int)` is True) and rejects fractional/
    negative values, which CockpitResponse's `commit_activity_4w: list[int]` can't represent
    faithfully (a fraction would silently truncate on Pydantic coercion; a negative would pass
    validation but produce a nonsensical metric)."""
    total = week.get("total", 0)
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise TypeError(f"invalid week total: {total!r}")
    return total


def _safe_commit_activity_4w_and_heatmap_52w(
    owner: str, token: str, repo_names: list[str]
) -> tuple[list[int], list[int], bool]:
    # Both windows are slices of the exact same GitHub call
    # (/repos/{owner}/{repo}/stats/commit_activity already returns 52 weeks), so this
    # fetches each repo once and derives both aggregates from it -- fetching twice
    # (once per aggregate) would double this endpoint's GitHub API cost for no reason.
    #
    # Each repo's future is resolved individually below: a failing repo is skipped (its
    # commits just don't contribute) rather than zeroing the whole org's aggregate, and the
    # returned `ok` flag tells the caller at least one repo's stats couldn't be fetched, so
    # the resulting totals are a real partial sum, not a silent lie dressed up as "0 commits."
    # No outer try/except: every future's result() is resolved inside the loop below, so a
    # per-repo httpx failure is always caught there -- an outer catch here would be dead code.
    client = GitHubClient(token)
    totals_4w = [0, 0, 0, 0]
    totals_52w = [0] * 52
    ok = True
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = [
            pool.submit(client.request, "GET", f"/repos/{owner}/{repo}/stats/commit_activity")
            for repo in repo_names[:_MAX_REPOS_FOR_AGGREGATES]
        ]
        for future in futures:
            try:
                weeks = future.result()
            except (httpx.HTTPStatusError, httpx.RequestError):
                ok = False
                continue
            if not isinstance(weeks, list):
                ok = False
                continue
            # A malformed nested record (a non-dict week, or a non-numeric "total") must not
            # raise past this point -- an uncaught exception here would escape asyncio.gather
            # and fail the whole cockpit request instead of just degrading this one repo's
            # contribution, defeating the whole point of this per-repo isolation. Accumulated
            # into a local delta first (not directly into totals_4w/52w) so a mid-repo failure
            # can't leave this one repo's contribution half-applied across the two arrays.
            try:
                delta_4w = [_week_total(week) for week in weeks[-4:]] if len(weeks) >= 4 else [0, 0, 0, 0]
                delta_52w = [_week_total(week) for week in weeks[-52:]] if len(weeks) >= 52 else [0] * 52
            except (AttributeError, TypeError):
                ok = False
                continue
            for i in range(4):
                totals_4w[i] += delta_4w[i]
            for i in range(52):
                totals_52w[i] += delta_52w[i]
    return totals_4w, totals_52w, ok


def _cache_entry_bytes(entry: dict) -> int:
    """Same contract and non-negative-plain-int rationale as _week_total: raises for the caller
    to treat as a per-repo failure instead of silently coercing a malformed entry into 0 bytes,
    accepting a bool, or accepting a negative/fractional size."""
    size = entry.get("size_in_bytes", 0)
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise TypeError(f"invalid cache entry size: {size!r}")
    return size


def _safe_total_cache_bytes(owner: str, token: str, repo_names: list[str]) -> tuple[int, bool]:
    # See _safe_commit_activity_4w_and_heatmap_52w's comment -- no outer try/except needed here
    # either, for the same reason.
    client = GitHubClient(token)
    total = 0
    ok = True
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = [
            pool.submit(client.request, "GET", f"/repos/{owner}/{repo}/actions/caches")
            for repo in repo_names[:_MAX_REPOS_FOR_AGGREGATES]
        ]
        for future in futures:
            try:
                data = future.result()
            except (httpx.HTTPStatusError, httpx.RequestError):
                ok = False
                continue
            if not isinstance(data, dict):
                ok = False
                continue
            # Same non-dict-entry/non-numeric-field guard as the commit-activity helper above --
            # a malformed cache entry must degrade this one repo, not raise past future.result()
            # and fail the whole cockpit request.
            try:
                total += sum(_cache_entry_bytes(c) for c in data.get("actions_caches", []))
            except (AttributeError, TypeError):
                ok = False
    return total, ok


def _milestone_state(due_on: str | None, progress_pct: float) -> str:
    if not due_on:
        return "on_track"
    try:
        due = datetime.fromisoformat(due_on.replace("Z", "+00:00"))
    except ValueError:
        return "on_track"
    now = datetime.now(timezone.utc)
    if due < now:
        return "overdue"
    if due - now < timedelta(days=7) and progress_pct < 70:
        return "at_risk"
    return "on_track"


def _safe_milestones(owner: str, token: str, repo_names: list[str]) -> tuple[list[MilestoneSummary], list[AtRiskRepo]]:
    """Fetches each repo's open milestones, best-effort per repo (one slow/broken repo
    doesn't blank out every other repo's milestones, unlike _safe_commit_activity_4w's
    all-or-nothing contract -- milestone data is naturally per-repo and independent)."""
    client = GitHubClient(token)
    milestones: list[MilestoneSummary] = []

    def _fetch(repo: str) -> list[dict]:
        try:
            return client.request("GET", f"/repos/{owner}/{repo}/milestones", params={"state": "open"})
        except (httpx.HTTPStatusError, httpx.RequestError):
            return []

    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(_fetch, repo): repo for repo in repo_names[:_MAX_REPOS_FOR_AGGREGATES]}
        for future, repo in futures.items():
            for m in future.result():
                open_issues = m.get("open_issues", 0)
                closed_issues = m.get("closed_issues", 0)
                total = open_issues + closed_issues
                progress_pct = round((closed_issues / total) * 100, 1) if total else 0.0
                due_on = m.get("due_on")
                milestones.append(
                    MilestoneSummary(
                        repo=repo,
                        title=m.get("title", ""),
                        due_on=due_on,
                        open_issues=open_issues,
                        closed_issues=closed_issues,
                        progress_pct=progress_pct,
                        state=_milestone_state(due_on, progress_pct),
                    )
                )

    milestones.sort(key=lambda m: (m.due_on is None, m.due_on))

    at_risk_by_repo: dict[str, AtRiskRepo] = {}
    for m in milestones:
        if m.state == "on_track":
            continue
        severity = "critical" if m.state == "overdue" else "warning"
        reason = (
            f"Milestone '{m.title}' overdue"
            if m.state == "overdue"
            else f"Milestone '{m.title}' due soon at {m.progress_pct:.0f}% complete"
        )
        existing = at_risk_by_repo.get(m.repo)
        if existing is None:
            at_risk_by_repo[m.repo] = AtRiskRepo(repo=m.repo, reasons=[reason], severity=severity)
        else:
            existing.reasons.append(reason)
            if severity == "critical":
                existing.severity = "critical"

    at_risk_repos = sorted(at_risk_by_repo.values(), key=lambda r: r.severity != "critical")
    return milestones[:10], at_risk_repos[:10]


def _week_pr_cycle_time(client: GitHubClient, owner: str, start: date) -> PrCycleTimeWeek:
    # closed_at approximates merge time for a merged PR (search API's issues endpoint
    # doesn't expose merged_at directly) -- an approximation, same spirit as Phase 18's
    # documented "last activity" sampling elsewhere in this codebase.
    # GitHub's search API date qualifiers are inclusive on both ends at day granularity,
    # so the window end is `+6 days` (a 7-day span) not `+7` -- otherwise a PR merged
    # exactly on a week-boundary day would double-count into both adjacent weeks.
    result = client.request(
        "GET",
        "/search/issues",
        params={"q": f"org:{owner} type:pr merged:{start}..{start + timedelta(days=6)}", "per_page": 30},
    )
    items = result.get("items", []) if isinstance(result, dict) else []
    days: list[float] = []
    for item in items:
        try:
            created = datetime.fromisoformat(item["created_at"].replace("Z", "+00:00"))
            closed = datetime.fromisoformat(item["closed_at"].replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError):
            continue
        days.append((closed - created).total_seconds() / 86400)
    avg_days = round(sum(days) / len(days), 1) if days else 0.0
    return PrCycleTimeWeek(week=start.isoformat(), avg_days=avg_days)


def _safe_pr_cycle_time_8w(owner: str, token: str) -> list[PrCycleTimeWeek]:
    try:
        client = GitHubClient(token)
        week_starts = [_week_start(weeks_ago) for weeks_ago in range(7, -1, -1)]
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(_week_pr_cycle_time, client, owner, start) for start in week_starts]
            return [f.result() for f in futures]
    except (httpx.HTTPStatusError, httpx.RequestError):
        return []


def _safe_release_cadence_4w(owner: str, token: str, repo_names: list[str]) -> list[int]:
    """Weekly release counts across the org's repos for the last 4 weeks -- a coarse
    KPI signal for the CEO cockpit, distinct in shape/purpose from the full per-release
    timeline docs/plan.md Phase 17 adds separately."""
    week_starts = [_week_start(weeks_ago) for weeks_ago in range(3, -1, -1)]
    totals = [0, 0, 0, 0]

    def _fetch(repo: str) -> list[dict]:
        try:
            client = GitHubClient(token)
            return client.request("GET", f"/repos/{owner}/{repo}/releases", params={"per_page": 20})
        except (httpx.HTTPStatusError, httpx.RequestError):
            return []

    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = [pool.submit(_fetch, repo) for repo in repo_names[:_MAX_REPOS_FOR_AGGREGATES]]
        for future in futures:
            releases = future.result()
            if not isinstance(releases, list):
                continue
            for r in releases:
                published_at = r.get("published_at")
                if not published_at:
                    continue
                try:
                    published = datetime.fromisoformat(published_at.replace("Z", "+00:00")).date()
                except ValueError:
                    continue
                for i, start in enumerate(week_starts):
                    if start <= published < start + timedelta(days=7):
                        totals[i] += 1
                        break
    return totals


def _cache_job_success_rate(db: Session) -> float:
    jobs = job_repo.list_recent_by_type(db, job_type=_CACHE_JOB_TYPE, limit=20)
    done = sum(1 for j in jobs if j["status"] == "done")
    failed = sum(1 for j in jobs if j["status"] == "failed")
    return done / (done + failed) if (done + failed) else 0.0


@router.get("/me/analytics/cockpit/{owner}", response_model=CockpitResponse)
async def personal_analytics_cockpit(
    owner: str,
    user: UserOut = Depends(require_auth),
    db: Session = Depends(get_db),
    x_github_token: str | None = Header(default=None),
):
    try:
        token = await anyio.to_thread.run_sync(
            lambda: resolve_owner_token(db, user_id=user.id, owner=owner, client_token=x_github_token)
        )
    except NoGitHubTokenAvailable as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    scans = scan_results_repo.list_recent(db, owner=owner, limit=10)
    latest_score = scans[0]["score"] if scans else None
    score_trend = [s["score"] for s in reversed(scans)]
    cache_job_success_rate = _cache_job_success_rate(db)
    connected_tenant_id = await anyio.to_thread.run_sync(lambda: _cockpit_connected_tenant(db, user.id, owner))

    try:
        repos = await anyio.to_thread.run_sync(lambda: _safe_list_repos(owner, token))
    except (httpx.HTTPStatusError, httpx.RequestError) as exc:
        raise _github_error(exc) from exc
    repo_names = [r["name"] for r in repos]

    (
        (member_count, member_count_ok),
        (recent_events, recent_events_degraded, recent_events_stale, (commit_activity_4w, commit_heatmap_52w, commit_activity_ok)),
        (open_pr_count, open_pr_count_ok),
        pr_merge_rate_4w,
        (total_cache_size_bytes, cache_bytes_ok),
        (milestones, at_risk_repos),
        pr_cycle_time_8w,
        release_cadence_4w,
    ) = await asyncio.gather(
        anyio.to_thread.run_sync(lambda: _safe_member_count(owner, token)),
        anyio.to_thread.run_sync(
            lambda: _cockpit_events_and_commit_activity(db, owner, token, connected_tenant_id, repo_names)
        ),
        anyio.to_thread.run_sync(lambda: _safe_open_pr_count(owner, token)),
        anyio.to_thread.run_sync(lambda: _safe_pr_merge_rate_4w(owner, token)),
        anyio.to_thread.run_sync(lambda: _safe_total_cache_bytes(owner, token, repo_names)),
        anyio.to_thread.run_sync(lambda: _safe_milestones(owner, token, repo_names)),
        anyio.to_thread.run_sync(lambda: _safe_pr_cycle_time_8w(owner, token)),
        anyio.to_thread.run_sync(lambda: _safe_release_cadence_4w(owner, token, repo_names)),
    )

    degraded = (
        not member_count_ok
        or not open_pr_count_ok
        or not cache_bytes_ok
        or not commit_activity_ok
        or recent_events_degraded
    )

    return CockpitResponse(
        repo_count=len(repos),
        member_count=member_count,
        latest_score=latest_score,
        score_trend=score_trend,
        recent_events=recent_events,
        open_pr_count=open_pr_count,
        pr_merge_rate_4w=pr_merge_rate_4w,
        commit_activity_4w=commit_activity_4w,
        commit_heatmap_52w=commit_heatmap_52w,
        total_cache_size_bytes=total_cache_size_bytes,
        cache_job_success_rate=cache_job_success_rate,
        at_risk_repos=at_risk_repos,
        milestones=milestones,
        pr_cycle_time_8w=pr_cycle_time_8w,
        release_cadence_4w=release_cadence_4w,
        commit_activity_source="aggregate" if connected_tenant_id is not None else "github",
        recent_events_source="aggregate" if connected_tenant_id is not None else "github",
        recent_events_stale=recent_events_stale,
        degraded=degraded,
    )


# ---------------------------------------------------------------------------
# My View (docs/plan.md Phase 14) -- a single GitHub-scoped account's own open PRs,
# review queue, assigned issues, and recent workflow runs, resolved via the same
# per-owner token as the cockpit. GitHub's search API works across every repo the
# token can see (not just `owner`'s), so my_open_prs/review_requests/assigned_issues
# aren't scoped to `owner` -- only the token-resolution step is.
# ---------------------------------------------------------------------------

_MAX_REPOS_FOR_RUN_LOOKUP = 15


def _my_login(client: GitHubClient, fallback_login: str | None = None) -> str | None:
    try:
        data = client.request("GET", "/user")
        return data.get("login") if isinstance(data, dict) else None
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 403:
            # Not the expected "installation token can't call /user" case -- a real
            # auth/server failure (401/404/5xx). Let it propagate rather than silently
            # masquerading as "this user has zero PRs/issues".
            raise
        # Installation (App) tokens aren't user-to-server tokens and get a 403 here --
        # fall back to the signed-in Clevis user's own GitHub OAuth-linked login (if any)
        # instead of silently returning zero PRs/issues for someone who really has some.
        # Still None (caller degrades to an empty response) if the user never linked
        # GitHub OAuth -- there's no other way to know who they are on GitHub.
        return fallback_login


def _search_items(client: GitHubClient, query: str, per_page: int = 10) -> list[dict]:
    try:
        result = client.request("GET", "/search/issues", params={"q": query, "per_page": per_page})
        return result.get("items", []) if isinstance(result, dict) else []
    except (httpx.HTTPStatusError, httpx.RequestError):
        return []


def _pr_summaries(items: list[dict]) -> list[PRSummary]:
    return [
        PRSummary(
            number=i["number"],
            title=i.get("title", ""),
            repository=i.get("repository_url", "").split("/repos/")[-1],
            html_url=i.get("html_url", ""),
            updated_at=i["updated_at"],
        )
        for i in items
        if "number" in i and "updated_at" in i
    ]


def _issue_summaries(items: list[dict]) -> list[IssueSummary]:
    return [
        IssueSummary(
            number=i["number"],
            title=i.get("title", ""),
            repository=i.get("repository_url", "").split("/repos/")[-1],
            html_url=i.get("html_url", ""),
            updated_at=i["updated_at"],
        )
        for i in items
        if "number" in i and "updated_at" in i
    ]


def _safe_my_recent_runs(client: GitHubClient, owner: str, login: str, repo_names: list[str]) -> list[RunSummaryLite]:
    def _fetch(repo: str) -> list[dict]:
        try:
            data = client.request(
                "GET", f"/repos/{owner}/{repo}/actions/runs", params={"actor": login, "per_page": 5}
            )
            return data.get("workflow_runs", []) if isinstance(data, dict) else []
        except (httpx.HTTPStatusError, httpx.RequestError):
            return []

    runs: list[RunSummaryLite] = []
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(_fetch, repo): repo for repo in repo_names[:_MAX_REPOS_FOR_RUN_LOOKUP]}
        for future, repo in futures.items():
            for r in future.result():
                runs.append(
                    RunSummaryLite(
                        repository=f"{owner}/{repo}",
                        id=r["id"],
                        name=r.get("name"),
                        status=r["status"],
                        conclusion=r.get("conclusion"),
                        html_url=r.get("html_url", ""),
                        created_at=r["created_at"],
                    )
                )
    runs.sort(key=lambda r: r.created_at, reverse=True)
    return runs[:10]


@router.get("/me/github/my-view", response_model=MyViewResponse)
async def my_view(
    owner: str,
    user: UserOut = Depends(require_auth),
    db: Session = Depends(get_db),
    x_github_token: str | None = Header(default=None),
):
    try:
        token = await anyio.to_thread.run_sync(
            lambda: resolve_owner_token(db, user_id=user.id, owner=owner, client_token=x_github_token)
        )
    except NoGitHubTokenAvailable as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    client = GitHubClient(token)
    login = await anyio.to_thread.run_sync(lambda: _my_login(client, user.github_login))
    if login is None:
        return MyViewResponse(identity_unresolved=True)

    try:
        repos = await anyio.to_thread.run_sync(lambda: _safe_list_repos(owner, token))
    except (httpx.HTTPStatusError, httpx.RequestError):
        repos = []
    repo_names = [r["name"] for r in repos]

    (my_open_prs_raw, review_requests_raw, assigned_issues_raw, my_recent_runs) = await asyncio.gather(
        anyio.to_thread.run_sync(lambda: _search_items(client, f"is:pr is:open author:{login}")),
        anyio.to_thread.run_sync(lambda: _search_items(client, f"is:pr is:open review-requested:{login}")),
        anyio.to_thread.run_sync(lambda: _search_items(client, f"is:issue is:open assignee:{login}")),
        anyio.to_thread.run_sync(lambda: _safe_my_recent_runs(client, owner, login, repo_names)),
    )

    return MyViewResponse(
        my_open_prs=_pr_summaries(my_open_prs_raw),
        review_requests=_pr_summaries(review_requests_raw),
        assigned_issues=_issue_summaries(assigned_issues_raw),
        my_recent_runs=my_recent_runs,
    )


# ---------------------------------------------------------------------------
# My PRs / My Reviews / My Issues -- dedicated paginated lists behind the "My
# PRs"/"My Reviews"/"My Issues" pages. Deliberately separate from my_view above:
# my_view is a fixed top-10 "glance" widget for the Overview page, while these
# support real Prev/Next pagination via GitHub search's total_count. Kept as
# their own functions/routes rather than reusing _search_items so my_view's
# existing behavior and tests aren't disturbed.
# ---------------------------------------------------------------------------

# GitHub's search API only ever returns the first 1000 results for a query
# (regardless of total_count) -- page*per_page beyond that always 422s, so we
# short-circuit to an empty page instead of passing that error through.
_MAX_SEARCH_RESULTS = 1000


def _search_items_page(client: GitHubClient, query: str, page: int, per_page: int) -> tuple[list[dict], int]:
    try:
        result = client.request("GET", "/search/issues", params={"q": query, "per_page": per_page, "page": page})
        if not isinstance(result, dict):
            return [], 0
        return result.get("items", []), result.get("total_count", 0)
    except (httpx.HTTPStatusError, httpx.RequestError):
        return [], 0


async def _my_items_list(
    db: Session,
    user: UserOut,
    owner: str,
    x_github_token: str | None,
    query_template: str,
    mapper,
    response_cls,
    page: int,
    per_page: int,
):
    try:
        token = await anyio.to_thread.run_sync(
            lambda: resolve_owner_token(db, user_id=user.id, owner=owner, client_token=x_github_token)
        )
    except NoGitHubTokenAvailable as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    client = GitHubClient(token)
    login = await anyio.to_thread.run_sync(lambda: _my_login(client, user.github_login))
    if login is None:
        return response_cls(page=page, per_page=per_page, identity_unresolved=True)
    if page * per_page > _MAX_SEARCH_RESULTS:
        # Beyond GitHub's reachable window -- report the capped total (not 0) so
        # page/lastPage math stays consistent instead of regressing to "Page N of 1".
        return response_cls(total_count=_MAX_SEARCH_RESULTS, page=page, per_page=per_page)

    query = query_template.format(login=login)
    items_raw, total_count = await anyio.to_thread.run_sync(lambda: _search_items_page(client, query, page, per_page))
    # Cap the reported total to what's actually reachable, so the UI's Next button
    # disables at the true boundary instead of this branch ever being hit from normal paging.
    return response_cls(
        items=mapper(items_raw), total_count=min(total_count, _MAX_SEARCH_RESULTS), page=page, per_page=per_page
    )


@router.get("/me/github/my-prs", response_model=MyPrListResponse)
async def my_prs(
    owner: str,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=100),
    user: UserOut = Depends(require_auth),
    db: Session = Depends(get_db),
    x_github_token: str | None = Header(default=None),
):
    return await _my_items_list(
        db, user, owner, x_github_token, "is:pr is:open author:{login}", _pr_summaries, MyPrListResponse, page, per_page
    )


@router.get("/me/github/my-reviews", response_model=MyPrListResponse)
async def my_reviews(
    owner: str,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=100),
    user: UserOut = Depends(require_auth),
    db: Session = Depends(get_db),
    x_github_token: str | None = Header(default=None),
):
    return await _my_items_list(
        db,
        user,
        owner,
        x_github_token,
        "is:pr is:open review-requested:{login}",
        _pr_summaries,
        MyPrListResponse,
        page,
        per_page,
    )


@router.get("/me/github/my-issues", response_model=MyIssueListResponse)
async def my_issues(
    owner: str,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=100),
    user: UserOut = Depends(require_auth),
    db: Session = Depends(get_db),
    x_github_token: str | None = Header(default=None),
):
    return await _my_items_list(
        db,
        user,
        owner,
        x_github_token,
        "is:issue is:open assignee:{login}",
        _issue_summaries,
        MyIssueListResponse,
        page,
        per_page,
    )
