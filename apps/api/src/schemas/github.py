from datetime import datetime

from pydantic import BaseModel, SecretStr


class OrgEventsInput(BaseModel):
    # Optional: falls back to a GitHub App installation token when one is connected
    # for this org (see src.services.token_resolution).
    token: SecretStr | None = None
    per_page: int = 30


class OrgEvent(BaseModel):
    id: str
    type: str
    actor: str
    actor_avatar: str
    repo: str
    summary: str
    created_at: datetime


class OrgEventsResponse(BaseModel):
    org: str
    events: list[OrgEvent]


class FailedRunsInput(BaseModel):
    token: SecretStr | None = None
    limit: int = 20


class FailedRunSummary(BaseModel):
    repo: str
    workflow_name: str
    branch: str
    run_id: int
    started_at: datetime
    duration_seconds: int | None
    url: str
    actor: str
    consecutive_failures: int


class FailedRunsResponse(BaseModel):
    org: str
    runs: list[FailedRunSummary]


class ReleaseTimelineInput(BaseModel):
    token: SecretStr | None = None
    days: int = 90


class ReleaseSummary(BaseModel):
    repo: str
    tag_name: str
    name: str
    published_at: datetime
    is_prerelease: bool
    body_preview: str
    url: str


class ReleaseTimelineResponse(BaseModel):
    org: str
    releases: list[ReleaseSummary]


class ActivitySummaryEntry(BaseModel):
    repo: str
    event_type: str
    count: int


class ActivitySummaryResponse(BaseModel):
    org: str
    days: int
    # False for a legacy PAT-only org (no GitHub App installation -> repo_event_daily_counts
    # is never populated for its tenant) -- totals is always [] in that case, not an error,
    # since a dashboard polling this should degrade gracefully rather than treat it as a
    # failure. See src.routers.github's org_activity_summary docstring.
    connected: bool
    generated_at: datetime
    totals: list[ActivitySummaryEntry]
