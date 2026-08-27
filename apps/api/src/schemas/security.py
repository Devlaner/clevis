from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class RepoSecurityRow(BaseModel):
    repo: str
    branch_protection: bool
    secret_scanning: bool
    dependabot_enabled: bool
    dependabot_critical_count: int
    dependabot_high_count: int
    code_scanning: bool
    force_push_allowed: bool
    score: int
    # "aggregate" when dependabot/code_scanning came from security_alerts (post-S6 PR 3)
    # instead of a live per-repo GitHub call -- branch_protection/force_push/secret_scanning
    # have no ingested event covering them and stay live either way, so this only
    # describes those two dimensions (dependabot_enabled is an approximation in the
    # "aggregate" case too -- see _dependabot_and_code_scanning_from_aggregate's docstring).
    alerts_source: Literal["github", "aggregate"] = "github"
    # Dimension names ("branch_protection", "dependabot", "code_scanning") the token
    # couldn't evaluate (403/429/network error) rather than genuinely observed as
    # compliant or not -- excluded from `score`'s denominator so a repo the token
    # can't see into isn't scored as if it were clean. Never includes a dimension
    # whose GitHub answer was a genuine 404 ("this feature is off"), which is a real
    # negative result, not an unknown.
    unknown_dimensions: list[str] = []


class VulnCounts(BaseModel):
    critical: int
    high: int
    medium: int
    low: int


class MatrixSummary(BaseModel):
    fully_compliant_count: int
    critical_risk_count: int
    secret_hits_count: int
    vuln_by_severity: VulnCounts


class SecurityMatrixResponse(BaseModel):
    owner: str
    repos: list[RepoSecurityRow]
    summary: MatrixSummary


class SecretAlert(BaseModel):
    # NOTE: the actual secret value is NEVER included here, only alert metadata.
    number: int
    state: str
    secret_type: str
    secret_type_display: str
    resolved_reason: str | None
    created_at: datetime
    resolved_at: datetime | None
    repo: str
    # None when no usable link exists -- the aggregate path (security_alerts) doesn't
    # store GitHub's html_url (CodeRabbit finding on PR #352: returning "" made every
    # aggregate-sourced alert render as a broken link instead of plain text).
    url: str | None


class SecretScanningResponse(BaseModel):
    repository: str
    alerts: list[SecretAlert]
    source: Literal["github", "aggregate"] = "github"
