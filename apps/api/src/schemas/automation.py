from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, SecretStr


class WorkflowSummary(BaseModel):
    # Overlay fields (last_run_*) are set via attribute assignment after construction
    # in the workflows router, not the constructor -- validate_assignment ensures the
    # raw GitHub API string still gets coerced to `datetime`.
    model_config = {"validate_assignment": True}

    id: int
    name: str
    path: str
    state: str
    last_run_status: str | None = None
    last_run_conclusion: str | None = None
    last_run_at: datetime | None = None


class WorkflowsResponse(BaseModel):
    repository: str
    workflows: list[WorkflowSummary]


class RunSummary(BaseModel):
    id: int
    name: str | None
    status: str
    conclusion: str | None
    head_branch: str
    created_at: datetime
    duration_ms: int | None = None


class RunsResponse(BaseModel):
    repository: str
    runs: list[RunSummary]


class DispatchInput(BaseModel):
    # Optional: falls back to a GitHub App installation token when one is connected
    # for this owner (see src.services.token_resolution).
    token: SecretStr | None = None
    # Bounded so a caller can't bloat the jobs/audit_logs payload columns (both
    # unbounded Text) with an arbitrarily large value. 255 is a generous cap for a git
    # ref name. GitHub's workflow_dispatch API documents a hard limit of 10 input keys;
    # 1024 chars per value isn't a documented GitHub limit, just a generous defensive cap.
    ref: str = Field(max_length=255)
    inputs: dict[str, Annotated[str, Field(max_length=1024)]] | None = Field(default=None, max_length=10)


class DispatchResponse(BaseModel):
    dispatched: bool
    message: str | None = None


class DispatchAllInput(BaseModel):
    # Same token fallback as DispatchInput. No per-workflow `inputs` -- a single
    # inputs dict can't sensibly apply across every workflow in the repo.
    token: SecretStr | None = None
    ref: str = Field(max_length=255)


class DispatchAllResult(BaseModel):
    workflow_id: int
    name: str
    # "skipped" = the workflow has no `workflow_dispatch` trigger (GitHub 422); this
    # is an expected outcome for a bulk fire, not a failure.
    status: Literal["dispatched", "skipped", "failed"]
    message: str | None = None


class DispatchAllResponse(BaseModel):
    ref: str
    results: list[DispatchAllResult]
    dispatched_count: int
    skipped_count: int
    failed_count: int
