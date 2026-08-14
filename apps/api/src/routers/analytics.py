import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import anyio
import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy.orm import Session

from src.core.auth import UserOut, require_auth
from src.core.db import get_db
from src.core.rbac import OrgContext, assert_owner_matches_org, require_org_role
from src.repositories import installation_repo, job_repo, org_repo, scan_results_repo, tenant_repo
from src.routers.github import _cached_events
from src.schemas.analytics import (
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


def _persist_scan(db: Session, result: dict, scanned_by_user_id: int | None = None) -> None:
    scan_results_repo.insert(
        db,
        owner=result["owner"],
        score=result["score"],
        total_checks=result["total_checks"],
        failed_checks=result["failed_checks"],
        checks=result["checks"],
        scanned_by_user_id=scanned_by_user_id,
    )


def _user_can_read_history(db: Session, user: UserOut, owner: str) -> bool:
    """Scan history isn't gated by GitHub-side authorization the way a live scan is
    (GitHub itself rejects a bad token/owner combo) -- it's a local DB read, so it
    needs its own access check. A user may read `owner`'s history if they're a
    member of the matching workspace Org, they personally have a GitHub App
    installation connected for that account login, or they're the one who ran a
    personal scan against that owner before (scanned_by_user_id, for owners with
    no workspace Org/membership at all -- the raw-PAT-paste flow)."""
    org = org_repo.get_by_login(db, owner)
    if org is not None:
        org = org_repo.ensure_tenant_linked(db, org)
        if tenant_repo.get_membership(db, org.tenant_id, user.id) is not None:
            return True
    if installation_repo.get_for_user(db, owner_user_id=user.id, account_login=owner) is not None:
        return True
    return scan_results_repo.exists_for_user(db, owner=owner, user_id=user.id)


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
    _persist_scan(db, result)
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
    _persist_scan(db, result, scanned_by_user_id=user.id)
    return result


@router.get("/orgs/{org_login}/analytics/history", response_model=list[ScanHistoryEntry])
def org_analytics_history(
    ctx: OrgContext = Depends(require_org_role(min_role="member")),
    db: Session = Depends(get_db),
):
    return scan_results_repo.list_recent(db, owner=ctx.org.github_login, limit=30)


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


def _safe_member_count(owner: str, token: str) -> int:
    try:
        client = GitHubClient(token)
        return len(client.request_paginated(f"/orgs/{owner}/members"))
    except (httpx.HTTPStatusError, httpx.RequestError):
        return 0


def _safe_recent_events(owner: str, token: str) -> list[OrgEventSummary]:
    try:
        events = _cached_events(owner, token, per_page=10).events
        return [OrgEventSummary(**e.model_dump()) for e in events[:5]]
    except (httpx.HTTPStatusError, httpx.RequestError, HTTPException):
        return []


def _week_start(weeks_ago: int) -> date:
    today = date.today()
    start_of_this_week = today - timedelta(days=today.weekday())
    return start_of_this_week - timedelta(weeks=weeks_ago)


def _search_count(client: GitHubClient, query: str) -> int:
    result = client.request("GET", "/search/issues", params={"q": query, "per_page": 1})
    return result.get("total_count", 0) if isinstance(result, dict) else 0


def _safe_open_pr_count(owner: str, token: str) -> int:
    try:
        client = GitHubClient(token)
        return _search_count(client, f"org:{owner} type:pr state:open")
    except (httpx.HTTPStatusError, httpx.RequestError):
        return 0


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


def _safe_commit_activity_4w_and_heatmap_52w(
    owner: str, token: str, repo_names: list[str]
) -> tuple[list[int], list[int]]:
    # Both windows are slices of the exact same GitHub call
    # (/repos/{owner}/{repo}/stats/commit_activity already returns 52 weeks), so this
    # fetches each repo once and derives both aggregates from it -- fetching twice
    # (once per aggregate) would double this endpoint's GitHub API cost for no reason.
    # A single failing repo zeroes both aggregates rather than partially summing --
    # simpler than reconciling "which repos contributed" and consistent with this
    # function's own all-or-nothing best-effort contract to its caller.
    try:
        client = GitHubClient(token)
        totals_4w = [0, 0, 0, 0]
        totals_52w = [0] * 52
        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = [
                pool.submit(client.request, "GET", f"/repos/{owner}/{repo}/stats/commit_activity")
                for repo in repo_names[:_MAX_REPOS_FOR_AGGREGATES]
            ]
            for future in futures:
                weeks = future.result()
                if not isinstance(weeks, list):
                    continue
                if len(weeks) >= 4:
                    for i, week in enumerate(weeks[-4:]):
                        totals_4w[i] += week.get("total", 0)
                if len(weeks) >= 52:
                    for i, week in enumerate(weeks[-52:]):
                        totals_52w[i] += week.get("total", 0)
        return totals_4w, totals_52w
    except (httpx.HTTPStatusError, httpx.RequestError):
        return [0, 0, 0, 0], [0] * 52


def _safe_total_cache_bytes(owner: str, token: str, repo_names: list[str]) -> int:
    try:
        client = GitHubClient(token)
        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = [
                pool.submit(client.request, "GET", f"/repos/{owner}/{repo}/actions/caches")
                for repo in repo_names[:_MAX_REPOS_FOR_AGGREGATES]
            ]
            results = [future.result() for future in futures]
            return sum(
                sum(c.get("size_in_bytes", 0) for c in data.get("actions_caches", []))
                for data in results
                if isinstance(data, dict)
            )
    except (httpx.HTTPStatusError, httpx.RequestError):
        return 0


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

    try:
        repos = await anyio.to_thread.run_sync(lambda: _safe_list_repos(owner, token))
    except (httpx.HTTPStatusError, httpx.RequestError) as exc:
        raise _github_error(exc) from exc
    repo_names = [r["name"] for r in repos]

    (
        member_count,
        recent_events,
        open_pr_count,
        pr_merge_rate_4w,
        (commit_activity_4w, commit_heatmap_52w),
        total_cache_size_bytes,
        (milestones, at_risk_repos),
        pr_cycle_time_8w,
        release_cadence_4w,
    ) = await asyncio.gather(
        anyio.to_thread.run_sync(lambda: _safe_member_count(owner, token)),
        anyio.to_thread.run_sync(lambda: _safe_recent_events(owner, token)),
        anyio.to_thread.run_sync(lambda: _safe_open_pr_count(owner, token)),
        anyio.to_thread.run_sync(lambda: _safe_pr_merge_rate_4w(owner, token)),
        anyio.to_thread.run_sync(lambda: _safe_commit_activity_4w_and_heatmap_52w(owner, token, repo_names)),
        anyio.to_thread.run_sync(lambda: _safe_total_cache_bytes(owner, token, repo_names)),
        anyio.to_thread.run_sync(lambda: _safe_milestones(owner, token, repo_names)),
        anyio.to_thread.run_sync(lambda: _safe_pr_cycle_time_8w(owner, token)),
        anyio.to_thread.run_sync(lambda: _safe_release_cadence_4w(owner, token, repo_names)),
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
    )


# ---------------------------------------------------------------------------
# My View (docs/plan.md Phase 14) -- a single GitHub-scoped account's own open PRs,
# review queue, assigned issues, and recent workflow runs, resolved via the same
# per-owner token as the cockpit. GitHub's search API works across every repo the
# token can see (not just `owner`'s), so my_open_prs/review_requests/assigned_issues
# aren't scoped to `owner` -- only the token-resolution step is.
# ---------------------------------------------------------------------------

_MAX_REPOS_FOR_RUN_LOOKUP = 15


def _my_login(client: GitHubClient) -> str | None:
    try:
        data = client.request("GET", "/user")
        return data.get("login") if isinstance(data, dict) else None
    except (httpx.HTTPStatusError, httpx.RequestError):
        # Installation (App) tokens aren't user-to-server tokens and can't call /user --
        # degrade to an empty MyViewResponse rather than 500 the whole page.
        return None


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
    login = await anyio.to_thread.run_sync(lambda: _my_login(client))
    if login is None:
        return MyViewResponse()

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
    login = await anyio.to_thread.run_sync(lambda: _my_login(client))
    if login is None:
        return response_cls(page=page, per_page=per_page)
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
