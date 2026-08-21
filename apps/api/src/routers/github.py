"""GET-equivalent org activity feed — proxies GitHub's `/orgs/{org}/events` and
normalizes each raw event into a human-readable summary (docs/plan.md Phase 9).

Implemented as POST (not the literal GET the plan doc sketches) so an optional
client-supplied PAT travels in the request body, never a URL/query string --
matching every other GitHub-token-bearing endpoint in this codebase
(src.routers.repos's *Input models).

Mounted with the full "/github/orgs/{org_login}/events" path on the router
itself (no `prefix=` passed to include_router in main.py), matching every
sibling org-scoped router's convention of defining its complete path locally.
"""

import hashlib
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from src.core.db import RepoEvent, RepoEventDailyCount, get_db
from src.core.rbac import OrgContext, require_org_role
from src.repositories import installation_repo
from src.schemas.github import (
    ActivitySummaryEntry,
    ActivitySummaryResponse,
    FailedRunsInput,
    FailedRunsResponse,
    FailedRunSummary,
    OrgEvent,
    OrgEventsInput,
    OrgEventsResponse,
    ReleaseSummary,
    ReleaseTimelineInput,
    ReleaseTimelineResponse,
)
from src.services.github_client import GitHubClient, github_error as _github_error
from src.services.token_resolution import NoGitHubTokenAvailable, resolve_org_token

# repo_events.event_type is the lowercase vocabulary apps/worker's event_consumer.py and
# backfill.py both write (issue #191/#192) -- the UI's event-feed.tsx filter chips match
# against GitHub's own raw PascalCase event type strings (e.g. "PushEvent"), the same
# strings the live-GitHub path below has always returned. Reversing the map here keeps
# the response contract identical regardless of which path served it.
_EVENT_TYPE_TO_GITHUB = {
    "push": "PushEvent",
    "pull_request": "PullRequestEvent",
    "issues": "IssuesEvent",
    "release": "ReleaseEvent",
    "create": "CreateEvent",
}

router = APIRouter()

# Each repo costs one additional GitHub call for failed-runs/release-timeline, on
# top of the initial repo list -- matching analytics.py's _MAX_REPOS_FOR_AGGREGATES
# tier for per-repo fan-out.
_MAX_REPOS_FOR_FEED = 20

# Short TTL, well under the frontend's 30s poll interval -- collapses concurrent
# polls from multiple open tabs/team members watching the same org into one
# upstream call, mirroring repos.py's _stats_cache for the same class of data
# (identical for every viewer of a given org at a given moment).
_EVENTS_CACHE_TTL_SECONDS = 25
_events_cache: dict[tuple[str, str, int], tuple[float, OrgEventsResponse]] = {}


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _is_bot(raw_event: dict) -> bool:
    login = (raw_event.get("actor") or {}).get("login", "")
    return login.endswith("[bot]")


def _summarize(raw_event: dict) -> str:
    event_type = raw_event.get("type", "")
    payload = raw_event.get("payload") or {}

    if event_type == "PushEvent":
        # GitHub truncates the embedded `commits` array to 20 entries even when
        # more were pushed -- `size` is the true total commit count.
        size = payload.get("size")
        commits = payload.get("commits") or []
        count = size if isinstance(size, int) else len(commits)
        branch = (payload.get("ref") or "").removeprefix("refs/heads/")
        noun = "commit" if count == 1 else "commits"
        return f"pushed {count} {noun} to {branch}" if branch else f"pushed {count} {noun}"

    if event_type == "PullRequestEvent":
        pr = payload.get("pull_request") or {}
        action = payload.get("action", "")
        verb = "merged" if action == "closed" and pr.get("merged") else action
        return f"{verb} PR #{payload.get('number')}: {pr.get('title', '')}"

    if event_type == "IssuesEvent":
        issue = payload.get("issue") or {}
        action = payload.get("action", "")
        return f"{action} issue #{issue.get('number')}: {issue.get('title', '')}"

    if event_type == "ReleaseEvent":
        release = payload.get("release") or {}
        return f"created release {release.get('tag_name', '')}"

    if event_type == "CreateEvent":
        ref_type = payload.get("ref_type", "")
        ref = payload.get("ref") or ""
        return f"created {ref_type} {ref}".strip()

    return event_type


def _normalize_event(raw_event: dict) -> OrgEvent:
    actor = raw_event.get("actor") or {}
    repo = raw_event.get("repo") or {}
    return OrgEvent(
        id=str(raw_event["id"]),
        type=raw_event.get("type", ""),
        actor=actor.get("login", ""),
        actor_avatar=actor.get("avatar_url", ""),
        repo=repo.get("name", ""),
        summary=_summarize(raw_event),
        created_at=raw_event["created_at"],
    )


def _fetch_events(org_login: str, token: str, per_page: int) -> OrgEventsResponse:
    client = GitHubClient(token)
    raw_events = client.request("GET", f"/orgs/{org_login}/events", params={"per_page": per_page})
    if not isinstance(raw_events, list):
        # GitHub's events endpoint always returns a JSON array; a dict here means
        # GitHubClient's empty-body fallback (`{}`) kicked in on an unexpected 2xx
        # response -- surface that as an error rather than silently rendering it
        # as "no events".
        raise HTTPException(status_code=502, detail="Unexpected response from GitHub events API")
    events = [_normalize_event(e) for e in raw_events if not _is_bot(e)]
    return OrgEventsResponse(org=org_login, events=events)


def _fetch_events_from_repo_events(db: Session, org_login: str, tenant_id: int, per_page: int) -> OrgEventsResponse:
    """S6: serves the Activity Feed from the already-populated, already-fresh repo_events
    table (S3 webhooks + S4 normalization + S5 install-time backfill/gap-healing) instead
    of a live GitHub call -- no token, no GitHub rate-limit exposure, no cache needed (a
    cheap indexed DB read doesn't need one). Relies on the tenant session context
    require_org_role's dependency already set (RLS, migration 0036) -- tenant_id is passed
    through explicitly only for the WHERE clause, not to re-establish that context.
    Bot-filtered here at read time, matching backfill.py's own note that repo_events itself
    stores every actor unfiltered by design (filtering is a display concern, not storage)."""
    rows = (
        db.query(RepoEvent)
        .filter(RepoEvent.tenant_id == tenant_id, ~RepoEvent.actor.like("%[bot]"))
        .order_by(RepoEvent.occurred_at.desc())
        .limit(per_page)
        .all()
    )
    events = [
        OrgEvent(
            id=str(row.id),
            type=_EVENT_TYPE_TO_GITHUB.get(row.event_type, row.event_type),
            actor=row.actor,
            actor_avatar=row.actor_avatar,
            repo=row.repo,
            summary=row.summary,
            created_at=row.occurred_at,
        )
        for row in rows
    ]
    return OrgEventsResponse(org=org_login, events=events)


def _evict_expired_events(now: float) -> None:
    # Same reasoning as repos.py's _evict_expired_stats: token_hash rotates hourly with
    # each fresh installation token, so stale keys would otherwise accumulate forever.
    expired = [key for key, (cached_at, _) in _events_cache.items() if now - cached_at >= _EVENTS_CACHE_TTL_SECONDS]
    for key in expired:
        del _events_cache[key]


def _cached_events(org_login: str, token: str, per_page: int) -> OrgEventsResponse:
    key = (org_login, _token_hash(token), per_page)
    now = time.monotonic()
    cached = _events_cache.get(key)
    if cached and now - cached[0] < _EVENTS_CACHE_TTL_SECONDS:
        return cached[1]
    events = _fetch_events(org_login, token, per_page)
    _events_cache[key] = (now, events)
    _evict_expired_events(now)
    return events


@router.post("/github/orgs/{org_login}/events", response_model=OrgEventsResponse)
def org_events(
    org_login: str,
    payload: OrgEventsInput,
    ctx: OrgContext = Depends(require_org_role(min_role="member")),
    db: Session = Depends(get_db),
):
    # repo_events only ever gets rows for a tenant with a connected GitHub App
    # installation -- webhooks, install-time backfill, and gap-healing (S3-S5) all require
    # one. An org still on the legacy PAT-only path has no installation and therefore no
    # repo_events rows; falling through to the live-GitHub path for that case (unchanged
    # below) avoids silently regressing it to an empty feed. Mirrors token_resolution.py's
    # own "prefer installation, fall back to client token" precedent, applied to the read
    # path instead of the auth path.
    #
    # get_for_org's own filter only matches on org_id/account_login (it's a general lookup,
    # also used to just display "is something connected" in list endpoints) -- a row can
    # exist with installation_id IS NULL (e.g. sync_org_installation's known-admin path lets
    # a caller re-sync org metadata without a real installation_id). Checking
    # installation_id here too, rather than widening get_for_org itself, mirrors
    # token_resolution.py's _from_installation, which guards the exact same gap at its own
    # call site instead of baking the check into the shared repository function.
    installation = installation_repo.get_for_org(db, org_id=ctx.org.id, account_login=org_login)
    if installation is not None and installation.installation_id is not None:
        return _fetch_events_from_repo_events(db, org_login, ctx.org.tenant_id, payload.per_page)

    client_token = payload.token.get_secret_value() if payload.token else None
    try:
        token = resolve_org_token(db, org_id=ctx.org.id, account_login=org_login, client_token=client_token)
    except NoGitHubTokenAvailable as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    try:
        return _cached_events(org_login, token, payload.per_page)
    except (httpx.HTTPStatusError, httpx.RequestError) as exc:
        raise _github_error(exc) from exc


# ---------------------------------------------------------------------------
# S6 foundation: first read path against a pre-computed aggregate table instead
# of repo_events row-by-row or a live GitHub call. repo_event_daily_counts
# (migration 0037, S4 PR 2) has been upserted by apps/worker's event_consumer.py
# and backfill.py since S4/S5 shipped, but nothing in apps/api has read it until
# now -- this is the "aggregates-first API" half of S6's own branch name
# (feat/aggregates-api-sse), proving the read + live-push pattern once on one
# real slice of data before the feature-module phases (8, 10/16, 11/18, 12/14,
# 13, 17) each build their own dashboard on top of it.
# ---------------------------------------------------------------------------

# Same reasoning as _MAX_REPOS_FOR_FEED below: an upper bound on a client-supplied
# window, not a default -- keeps a mistaken/malicious `days=100000` from summing an
# unbounded number of daily-count rows.
_ACTIVITY_SUMMARY_MAX_DAYS = 90
_ACTIVITY_SUMMARY_DEFAULT_DAYS = 7

# SSE poll cadence and a hard cap on how long a single stream connection stays open
# (v1: poll-and-diff against the aggregate table, not Postgres LISTEN/NOTIFY or a
# Redis pub/sub channel -- simplest thing that proves live-push works; a client
# whose connection hits the cap just reconnects, same as any short-lived SSE
# gateway timeout would force anyway).
_SSE_POLL_INTERVAL_SECONDS = 5
_SSE_MAX_DURATION_SECONDS = 15 * 60


def _org_installation_connected(db: Session, org_login: str, ctx: OrgContext) -> bool:
    # Same installation_id-presence guard as org_events above (see that handler's
    # comment) -- repo_event_daily_counts is upserted by the same pipeline that
    # populates repo_events, so it's only ever non-empty for a tenant with a real
    # connected installation.
    installation = installation_repo.get_for_org(db, org_id=ctx.org.id, account_login=org_login)
    return installation is not None and installation.installation_id is not None


def _activity_summary_snapshot(db: Session, org_login: str, ctx: OrgContext, days: int) -> ActivitySummaryResponse:
    connected = _org_installation_connected(db, org_login, ctx)
    totals: list[ActivitySummaryEntry] = []
    if connected:
        cutoff = date.today() - timedelta(days=days - 1)
        rows = (
            db.query(RepoEventDailyCount.repo, RepoEventDailyCount.event_type, func.sum(RepoEventDailyCount.count))
            .filter(RepoEventDailyCount.tenant_id == ctx.org.tenant_id, RepoEventDailyCount.day >= cutoff)
            .group_by(RepoEventDailyCount.repo, RepoEventDailyCount.event_type)
            .all()
        )
        totals = [ActivitySummaryEntry(repo=repo, event_type=event_type, count=int(count)) for repo, event_type, count in rows]
    return ActivitySummaryResponse(
        org=org_login, days=days, connected=connected, generated_at=datetime.now(timezone.utc), totals=totals
    )


def _activity_summary_stream(db: Session, org_login: str, ctx: OrgContext, days: int, poll_interval: float = _SSE_POLL_INTERVAL_SECONDS):
    """SSE body generator. Runs in Starlette's threadpool iterator (this route is a plain
    `def`, matching every other handler in this file, so FastAPI already executes it off
    the event loop) -- the blocking time.sleep below does not stall other requests.

    Only emits a real `activity_summary` event when the aggregate actually changed since
    the last poll (comparing on totals/connected, not generated_at, which changes every
    poll by definition); otherwise emits an SSE comment as a heartbeat, both to keep
    intermediary proxies from closing an idle-looking connection and to give the client a
    liveness signal distinct from "no events yet"."""
    deadline = time.monotonic() + _SSE_MAX_DURATION_SECONDS
    last_key: tuple | None = None
    while time.monotonic() < deadline:
        snapshot = _activity_summary_snapshot(db, org_login, ctx, days)
        key = (snapshot.connected, tuple(sorted((t.repo, t.event_type, t.count) for t in snapshot.totals)))
        if key != last_key:
            yield f"event: activity_summary\ndata: {snapshot.model_dump_json()}\n\n"
            last_key = key
        else:
            yield ": heartbeat\n\n"
        time.sleep(poll_interval)


@router.get("/github/orgs/{org_login}/activity-summary", response_model=ActivitySummaryResponse)
def org_activity_summary(
    org_login: str,
    days: int = _ACTIVITY_SUMMARY_DEFAULT_DAYS,
    ctx: OrgContext = Depends(require_org_role(min_role="member")),
    db: Session = Depends(get_db),
):
    """Per-repo, per-event-type event counts over a trailing window, read from the
    pre-computed repo_event_daily_counts rollup -- no live GitHub call, no token. GET (not
    POST like org_events/failed-runs/release-timeline) because, unlike those, this never
    takes a client-supplied token in its body -- there is nothing here that needs to avoid
    a URL/query string.

    Returns connected=False with empty totals (200, not an error) for a legacy PAT-only
    org that has no GitHub App installation -- repo_event_daily_counts is only ever
    populated for a tenant with one, same gating as org_events' repo_events path."""
    days = max(1, min(days, _ACTIVITY_SUMMARY_MAX_DAYS))
    return _activity_summary_snapshot(db, org_login, ctx, days)


@router.get("/github/orgs/{org_login}/activity-summary/stream")
def org_activity_summary_stream(
    org_login: str,
    days: int = _ACTIVITY_SUMMARY_DEFAULT_DAYS,
    ctx: OrgContext = Depends(require_org_role(min_role="member")),
    db: Session = Depends(get_db),
):
    """SSE channel pushing the same shape org_activity_summary returns, whenever it
    changes. First concrete piece of S6's "+ SSE" half -- see _activity_summary_stream."""
    days = max(1, min(days, _ACTIVITY_SUMMARY_MAX_DAYS))
    return StreamingResponse(_activity_summary_stream(db, org_login, ctx, days), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# Full developer feed (docs/plan.md Phase 17) -- org-wide failed-run log and
# release timeline, both fanned out per-repo (best-effort per repo, capped
# repo count), matching analytics.py's per-repo aggregate helper pattern.
# ---------------------------------------------------------------------------


def _run_duration_seconds(run: dict) -> int | None:
    started = run.get("run_started_at")
    updated = run.get("updated_at")
    if not started or not updated or run.get("status") != "completed":
        return None
    try:
        start_dt = datetime.fromisoformat(started.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(updated.replace("Z", "+00:00"))
    except ValueError:
        return None
    delta = int((end_dt - start_dt).total_seconds())
    return delta if delta >= 0 else None


def _repo_failed_runs(client: GitHubClient, owner: str, repo: str) -> list[FailedRunSummary]:
    try:
        data = client.request("GET", f"/repos/{owner}/{repo}/actions/runs", params={"per_page": 30})
    except (httpx.HTTPStatusError, httpx.RequestError):
        return []
    raw_runs = data.get("workflow_runs", []) if isinstance(data, dict) else []

    # GitHub returns runs newest-first; group by workflow to walk each workflow's
    # own timeline independently (a failing workflow on one runs list mustn't count
    # a different workflow's success as breaking its streak).
    by_workflow: dict[int, list[dict]] = {}
    for run in raw_runs:
        by_workflow.setdefault(run.get("workflow_id"), []).append(run)

    summaries: list[FailedRunSummary] = []
    for runs in by_workflow.values():
        streak = 0
        for run in runs:
            if run.get("status") == "completed" and run.get("conclusion") == "failure":
                streak += 1
            else:
                break
        if streak < 3:
            continue
        latest = runs[0]
        # A malformed run entry (missing id/created_at) shouldn't 500 the whole repo's
        # results -- skip just that workflow's streak, same as _pr_summaries/_issue_summaries
        # in analytics.py degrade on a malformed search-result item.
        if "id" not in latest or ("run_started_at" not in latest and "created_at" not in latest):
            continue
        summaries.append(
            FailedRunSummary(
                repo=f"{owner}/{repo}",
                workflow_name=latest.get("name") or "",
                branch=latest.get("head_branch", ""),
                run_id=latest["id"],
                started_at=latest.get("run_started_at") or latest.get("created_at"),
                duration_seconds=_run_duration_seconds(latest),
                url=latest.get("html_url", ""),
                actor=(latest.get("actor") or {}).get("login", ""),
                consecutive_failures=streak,
            )
        )
    return summaries


def _repo_releases(client: GitHubClient, owner: str, repo: str, cutoff: datetime) -> list[ReleaseSummary]:
    try:
        raw = client.request("GET", f"/repos/{owner}/{repo}/releases", params={"per_page": 20})
    except (httpx.HTTPStatusError, httpx.RequestError):
        return []
    if not isinstance(raw, list):
        return []

    releases = []
    for r in raw:
        published_at = r.get("published_at")
        if not published_at:
            continue
        try:
            published = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
        except ValueError:
            continue
        if published < cutoff:
            continue
        body = (r.get("body") or "")[:120]
        releases.append(
            ReleaseSummary(
                repo=f"{owner}/{repo}",
                tag_name=r.get("tag_name", ""),
                name=r.get("name") or r.get("tag_name", ""),
                published_at=published_at,
                is_prerelease=bool(r.get("prerelease")),
                body_preview=body,
                url=r.get("html_url", ""),
            )
        )
    return releases


@router.post("/github/orgs/{org_login}/failed-runs", response_model=FailedRunsResponse)
def org_failed_runs(
    org_login: str,
    payload: FailedRunsInput,
    ctx: OrgContext = Depends(require_org_role(min_role="member")),
    db: Session = Depends(get_db),
):
    client_token = payload.token.get_secret_value() if payload.token else None
    try:
        token = resolve_org_token(db, org_id=ctx.org.id, account_login=org_login, client_token=client_token)
    except NoGitHubTokenAvailable as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    client = GitHubClient(token)
    try:
        repos = client.request_paginated(f"/orgs/{org_login}/repos", params={"type": "all", "sort": "pushed"})
    except (httpx.HTTPStatusError, httpx.RequestError) as exc:
        raise _github_error(exc) from exc
    repo_names = [r["name"] for r in repos[:_MAX_REPOS_FOR_FEED]]

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = pool.map(lambda name: _repo_failed_runs(client, org_login, name), repo_names)
    runs = [r for repo_runs in results for r in repo_runs]
    runs.sort(key=lambda r: r.started_at, reverse=True)
    return FailedRunsResponse(org=org_login, runs=runs[: payload.limit])


@router.post("/github/orgs/{org_login}/release-timeline", response_model=ReleaseTimelineResponse)
def org_release_timeline(
    org_login: str,
    payload: ReleaseTimelineInput,
    ctx: OrgContext = Depends(require_org_role(min_role="member")),
    db: Session = Depends(get_db),
):
    client_token = payload.token.get_secret_value() if payload.token else None
    try:
        token = resolve_org_token(db, org_id=ctx.org.id, account_login=org_login, client_token=client_token)
    except NoGitHubTokenAvailable as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    client = GitHubClient(token)
    try:
        repos = client.request_paginated(f"/orgs/{org_login}/repos", params={"type": "all", "sort": "pushed"})
    except (httpx.HTTPStatusError, httpx.RequestError) as exc:
        raise _github_error(exc) from exc
    repo_names = [r["name"] for r in repos[:_MAX_REPOS_FOR_FEED]]
    cutoff = datetime.now(timezone.utc) - timedelta(days=payload.days)

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = pool.map(lambda name: _repo_releases(client, org_login, name, cutoff), repo_names)
    releases = [r for repo_releases in results for r in repo_releases]
    releases.sort(key=lambda r: r.published_at, reverse=True)
    return ReleaseTimelineResponse(org=org_login, releases=releases)
