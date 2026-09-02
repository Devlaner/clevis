from datetime import datetime
from typing import Literal

from pydantic import BaseModel, SecretStr


class AnalyticsInput(BaseModel):
    owner: str
    # Optional: falls back to a GitHub App installation token when one is connected
    # for this owner (see src.services.token_resolution).
    token: SecretStr | None = None


class CheckResult(BaseModel):
    """One security-check result as produced by ``checks.runner.run_all_checks``.

    Typed so a shape drift in ``packages/checks`` fails fast at the API boundary with a
    clear validation error instead of silently reaching the UI and crashing a render
    (issue #370). ``value`` is deliberately a union of every shape the six checks and the
    runner's error paths emit: a bare bool (MFA), a ``{str: int}`` counts dict (all the
    repo-level checks), or a plain string (runner-level failure messages).

    ``severity`` stays a free ``str`` on purpose: it's the source-of-truth
    ``CheckMetadata.severity`` (unconstrained), it's only a cosmetic chip in the UI, and
    ``github_checks.py`` already uses a wider vocabulary ("critical") elsewhere -- pinning
    it here would turn a new check's severity label into a 500 on the whole overview.
    """

    id: str
    title: str
    severity: str
    remediation: str
    status: Literal["pass", "fail", "error", "not_applicable"]
    value: bool | str | dict[str, int] | None = None


class AnalyticsResponse(BaseModel):
    owner: str
    score: int
    total_checks: int
    failed_checks: int
    repo_count: int
    checks: list[CheckResult]


class ScanHistoryEntry(BaseModel):
    id: int
    owner: str
    score: int
    total_checks: int
    failed_checks: int
    created_at: datetime


class OrgEventSummary(BaseModel):
    id: str
    type: str
    actor: str
    actor_avatar: str
    repo: str
    summary: str
    created_at: datetime


class PrWeekBucket(BaseModel):
    week: str
    opened: int
    merged: int


class AtRiskRepo(BaseModel):
    repo: str
    reasons: list[str]
    severity: Literal["warning", "critical"]


class MilestoneSummary(BaseModel):
    repo: str
    title: str
    due_on: datetime | None
    open_issues: int
    closed_issues: int
    progress_pct: float
    state: Literal["on_track", "at_risk", "overdue"]


class PrCycleTimeWeek(BaseModel):
    week: str
    avg_days: float


class CockpitResponse(BaseModel):
    repo_count: int
    member_count: int
    latest_score: int | None
    score_trend: list[int]
    recent_events: list[OrgEventSummary]
    open_pr_count: int
    pr_merge_rate_4w: list[PrWeekBucket]
    commit_activity_4w: list[int]
    total_cache_size_bytes: int
    cache_job_success_rate: float
    commit_heatmap_52w: list[int] = []
    at_risk_repos: list[AtRiskRepo] = []
    milestones: list[MilestoneSummary] = []
    pr_cycle_time_8w: list[PrCycleTimeWeek] = []
    release_cadence_4w: list[int] = []
    commit_activity_source: Literal["github", "aggregate"] = "github"
    recent_events_source: Literal["github", "aggregate"] = "github"
    # True if a stored aggregate is stale relative to gap_heal_stale_hours -- only ever set when
    # recent_events_source == "aggregate"; the live "github" path has no ingestion cursor to be
    # stale against (whatever GitHub returns synchronously is definitionally current).
    recent_events_stale: bool = False
    # True if any best-effort live GitHub call underlying this response failed and fell back to a
    # zero/empty/partial value (see the _safe_* helpers below) -- lets the UI distinguish "this org
    # genuinely has none" from "we couldn't fully fetch this," which previously rendered identically.
    degraded: bool = False


class PRSummary(BaseModel):
    number: int
    title: str
    repository: str
    html_url: str
    updated_at: datetime


class IssueSummary(BaseModel):
    number: int
    title: str
    repository: str
    html_url: str
    updated_at: datetime


class RunSummaryLite(BaseModel):
    repository: str
    id: int
    name: str | None
    status: str
    conclusion: str | None
    html_url: str
    created_at: datetime


class MyViewResponse(BaseModel):
    my_open_prs: list[PRSummary] = []
    review_requests: list[PRSummary] = []
    assigned_issues: list[IssueSummary] = []
    my_recent_runs: list[RunSummaryLite] = []


class MyPrListResponse(BaseModel):
    items: list[PRSummary] = []
    total_count: int = 0
    page: int = 1
    per_page: int = 25


class MyIssueListResponse(BaseModel):
    items: list[IssueSummary] = []
    total_count: int = 0
    page: int = 1
    per_page: int = 25
