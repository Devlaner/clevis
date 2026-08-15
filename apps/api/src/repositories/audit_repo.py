import json

from sqlalchemy import text
from sqlalchemy.orm import Session

from src.core.db import AuditLog


def write(db: Session, actor: str, action: str, target: str, payload: dict, tenant_id: int | None = None) -> None:
    # Issue #330: audit_logs' RLS policy is strict equality against app.tenant_id (see
    # migration 0030's docstring -- deliberately no OR-NULL escape, so a NULL-tenant row
    # can never be inserted going forward). Many callers of this function (personal-scoped
    # routes especially) have no other reason to ever call
    # rbac.set_tenant_session_context, so app.tenant_id may not be set to the right value
    # -- or set at all -- when this INSERT runs. SET LOCAL here to exactly the value being
    # written is always safe (it's not a caller-controlled escalation, just making the
    # session context match the row this call is already about to write) and scoped to
    # only this transaction, so it doesn't leak into whatever the caller's session does
    # next.
    if tenant_id is not None:
        db.execute(text(f"SET LOCAL app.tenant_id = {int(tenant_id)}"))
    db.add(AuditLog(actor=actor, action=action, target=target, payload=json.dumps(payload), tenant_id=tenant_id))
    db.commit()
