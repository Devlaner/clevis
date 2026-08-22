from datetime import datetime
from typing import Literal

from pydantic import BaseModel, SecretStr


class AnalyticsInput(BaseModel):
    owner: str
    # Optional: falls back to a GitHub App installation token when one is connected
    # for this owner (see src.services.token_resolution).
    token: SecretStr | None = None


class AnalyticsResponse(BaseModel):
    owner: str
    score: int
    total_checks: int
    failed_checks: int
    repo_count: int
    checks: list[dict]


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
