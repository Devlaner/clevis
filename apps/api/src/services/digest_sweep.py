"""Issue #292: per-tick work for the leadership-digest loop.

Mirrors gap_heal_sweep.py's shape (see that module's docstring). Scope: org-kind
tenants only (a personal tenant has no leadership to summarise for). Opt-in and
off by default -- controlled by the `digest_cadence` instance-config key
(off | weekly | monthly). "Is a digest due" is derived from the newest
`digest.sent` audit-log row for the tenant, so no new table is needed.
"""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

from src.core.app_config import get_config
from src.core.db import set_session_tenant
from src.repositories import audit_repo
from src.services import digest_service, email
from src.services.sweep_lock import try_acquire_sweep_slot

logger = logging.getLogger(__name__)

_SWEEP_KEY = "digest"
_CADENCE_DAYS = {"weekly": 7, "monthly": 30}


def _read_cadence() -> str | None:
    raw = (get_config("digest_cadence", "off") or "off").strip().lower()
    return raw if raw in _CADENCE_DAYS else None


def _recipients(db: Session, tenant_id: int) -> list[str]:
    rows = db.execute(
        text(
            "SELECT u.email FROM memberships m JOIN users u ON u.id = m.user_id "
            "WHERE m.tenant_id = :t AND m.role = 'admin' "
            "AND u.email IS NOT NULL AND u.email_verified = true"
        ),
        {"t": tenant_id},
    ).fetchall()
    return [r[0] for r in rows]


def _last_sent_at(db: Session, tenant_id: int) -> datetime | None:
    row = db.execute(
        text(
            "SELECT MAX(created_at) FROM audit_logs "
            "WHERE tenant_id = :t AND action = 'digest.sent'"
        ),
        {"t": tenant_id},
    ).fetchone()
    return row[0] if row else None


def run_digest_sweep(db: Session) -> None:
    cadence = _read_cadence()
    if cadence is None:
        return
    if not email.is_configured():
        logger.info("digest sweep: cadence=%s but SMTP is not configured; nothing sent", cadence)
        return

    interval_days = _CADENCE_DAYS[cadence]
    now = datetime.now(timezone.utc)

    # tenants has no RLS (identity table) -- read freely, then scope per tenant below.
    tenants = db.execute(
        text("SELECT t.id, o.github_login FROM tenants t JOIN orgs o ON o.id = t.org_id WHERE t.kind = 'org'")
    ).fetchall()

    for tenant_id, org_login in tenants:
        try:
            set_session_tenant(db, tenant_id)

            # Serialise the check-then-send window across replicas (see sweep_lock.py).
            if not try_acquire_sweep_slot(db, _SWEEP_KEY, tenant_id):
                continue

            # Read the last-sent marker *after* taking the slot. Another replica may
            # have sent and committed its digest.sent row between this tick's tenant
            # query and now; checking before the lock lets both replicas past.
            last_sent = _last_sent_at(db, tenant_id)
            if last_sent is not None:
                if last_sent.tzinfo is None:
                    last_sent = last_sent.replace(tzinfo=timezone.utc)
                # 12h grace so the digest doesn't drift a poll-interval later every
                # period (timedelta.days floors; the send only lands on a poll tick).
                if now - last_sent < timedelta(days=interval_days, hours=-12):
                    db.commit()  # release the advisory slot; not due yet
                    continue

            recipients = _recipients(db, tenant_id)
            if not recipients:
                db.commit()  # release the advisory lock; nothing to do for this tenant
                continue

            content = digest_service.build_digest(
                db, tenant_id=tenant_id, org_login=org_login, period_label=cadence
            )
            if content.is_empty:
                db.commit()
                continue

            subject = digest_service.render_subject(content)
            body = digest_service.render_text(content)
            # is_configured() was checked at the top of the sweep. If SMTP drops
            # mid-run a send just fails like any other error -- caught here so a
            # partial success still records its digest.sent marker (otherwise the
            # next tick re-sends to everyone, including admins already emailed).
            sent = 0
            for address in recipients:
                try:
                    email.send_email(address, subject, body)
                    sent += 1
                except Exception:
                    logger.exception("digest sweep: failed to email %s for tenant %d", address, tenant_id)

            if sent:
                audit_repo.write(
                    db,
                    actor="system:digest",
                    action="digest.sent",
                    target=org_login,
                    payload={"cadence": cadence, "recipients": sent, "score": content.latest_score},
                    tenant_id=tenant_id,
                )
            else:
                db.commit()  # release the lock even when every send failed
        except Exception:
            db.rollback()
            logger.exception("digest sweep: iteration failed for tenant %d", tenant_id)
