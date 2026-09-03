"""Read/write helpers for ``automation_repo_settings`` — the per-(tenant, repo, feature)
opt-in + options store shared by bulk branch protection (#288) and Dependabot triage
(#290).

RLS scopes every row by ``tenant_id``; callers must have set the tenant session context
(``rbac.set_tenant_session_context``) before these run under the constrained API role.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.core.db import AutomationRepoSetting


def get(db: Session, tenant_id: int, repo: str, feature: str) -> AutomationRepoSetting | None:
    return db.execute(
        select(AutomationRepoSetting).where(
            AutomationRepoSetting.tenant_id == tenant_id,
            AutomationRepoSetting.repo == repo,
            AutomationRepoSetting.feature == feature,
        )
    ).scalar_one_or_none()


def list_for_feature(db: Session, tenant_id: int, feature: str) -> list[AutomationRepoSetting]:
    return list(
        db.execute(
            select(AutomationRepoSetting)
            .where(
                AutomationRepoSetting.tenant_id == tenant_id,
                AutomationRepoSetting.feature == feature,
            )
            .order_by(AutomationRepoSetting.repo)
        ).scalars()
    )


def upsert(
    db: Session,
    tenant_id: int,
    repo: str,
    feature: str,
    *,
    enabled: bool,
    mode: str | None = None,
    extra: dict | None = None,
) -> AutomationRepoSetting:
    row = get(db, tenant_id, repo, feature)
    if row is None:
        row = AutomationRepoSetting(tenant_id=tenant_id, repo=repo, feature=feature)
        db.add(row)
    row.enabled = enabled
    row.mode = mode
    if extra is not None:
        row.extra = extra
    db.flush()
    return row
