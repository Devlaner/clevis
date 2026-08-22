"""GitHub App webhook receiver.

  POST /webhooks/github   verifies X-Hub-Signature-256, then handles installation
                           lifecycle events to keep github_installations in sync, and
                           durably queues a bounded set of event types (issue #191/S3)
                           for a future event-processor fleet (S4, not built yet).
"""

import hashlib
import hmac
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from src.core.config import settings
from src.core.db import WebhookDelivery, get_db
from src.core.rate_limit import rate_limit
from src.core.redis_client import get_redis_client
from src.repositories import audit_repo, installation_repo

logger = logging.getLogger(__name__)

router = APIRouter()

# Two families of durably-queued event types (X-GitHub-Event header names, not GitHub
# Events API `type` strings, hence "create" not "CreateEvent"):
#   - Activity Feed events (push/pull_request/issues/release/create) -- normalized by
#     S4's event-processor fleet into repo_events/repo_event_daily_counts.
#   - Security alert events (dependabot_alert/code_scanning_alert/secret_scanning_alert,
#     added for the Security dashboard's per-repo compliance matrix + alerts panel,
#     issue #191-follow-on) -- normalized into security_alerts as of post-S6 PR 2.
#     Requires the Clevis GitHub App's own webhook subscriptions + permissions
#     (Dependabot alerts: read, Code scanning alerts: read, Secret scanning alerts:
#     read) to be turned on for GitHub to actually send these -- see
#     docs/self-hosting.md.
#   - Org membership / repo access events (member/organization/membership/team, added
#     for the Collaborators dashboard, post-S6 stage) -- landed here durably as of this
#     PR; member/organization are normalized into org_members/repo_collaborators (see
#     event_consumer.py), membership/team are acked-but-skipped for now (team-based
#     repo access is deferred, see that module's docstring). Requires the Clevis
#     GitHub App's own webhook subscriptions + permissions (Members: read/write,
#     Organization administration or similar per GitHub's docs) -- see
#     docs/self-hosting.md.
# Either way, an ingested event just accumulates in webhook_deliveries with
# status="queued" until its consumer exists -- that's the expected handoff state, not
# a bug.
_INGESTED_EVENT_TYPES = {
    "push",
    "pull_request",
    "issues",
    "release",
    "create",
    "dependabot_alert",
    "code_scanning_alert",
    "secret_scanning_alert",
    "member",
    "organization",
    "membership",
    "team",
}

# Redis Stream key the ingestion path XADDs onto; a future S4 consumer group reads
# from this same key.
_WEBHOOK_STREAM_KEY = "webhook_events"

# GitHub caps webhook deliveries around 25MB; refuse anything larger long before that
# so a body this endpoint (unauthenticated until the signature check passes) can't be
# used to exhaust memory. Enforced by counting streamed bytes, not by trusting a
# client-supplied Content-Length header.
_MAX_BODY_BYTES = 5 * 1024 * 1024


async def _read_bounded_body(request: Request) -> bytes:
    chunks = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > _MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="Webhook payload too large")
        chunks.append(chunk)
    return b"".join(chunks)


def _verify_signature(raw_body: bytes, signature_header: str | None, secret: str) -> bool:
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    provided = signature_header.removeprefix("sha256=")
    # Compare as bytes, not str — hmac.compare_digest raises TypeError on a str
    # containing non-ASCII characters, which an attacker fully controls via this
    # header; encoding first keeps a malformed signature a clean 401, not a 500.
    return hmac.compare_digest(expected.encode(), provided.encode("utf-8", errors="replace"))


# Generous relative to the auth-endpoint default (10/60s): GitHub's webhook deliveries
# for many customers' installations can share the same egress IP, so a strict per-IP
# limit risks throttling legitimate traffic. Still bounds worst-case abuse from any
# single source.
@router.post("/webhooks/github", dependencies=[Depends(rate_limit(max_requests=120, window_seconds=60))])
async def github_webhook(request: Request, db: Session = Depends(get_db)):
    secret = settings.github_app_webhook_secret
    if not secret:
        raise HTTPException(status_code=503, detail="GitHub webhook secret not configured")

    raw_body = await _read_bounded_body(request)
    if not _verify_signature(raw_body, request.headers.get("X-Hub-Signature-256"), secret.get_secret_value()):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Malformed webhook payload")

    event = request.headers.get("X-GitHub-Event", "")
    delivery_id = request.headers.get("X-GitHub-Delivery", "")

    if event == "installation" and payload.get("action") == "deleted":
        _handle_installation_deleted(db, payload)
    # installation.created is deliberately a no-op: rows are only ever written to
    # github_installations by the authenticated /orgs/{org}/installations/sync and
    # /me/installations/sync endpoints, which independently verify (via
    # _verify_installation, see #141) that the installation_id genuinely belongs to
    # the claimed account before persisting. Auto-creating a row straight from this
    # webhook would mean trusting GitHub's payload to decide which clevis org/user it
    # belongs to with no equivalent ownership check — that's a bigger trust-boundary
    # decision than "keep an existing verified row in sync," so it's left to the
    # explicit sync flow rather than done implicitly here.
    #
    # installation_repositories (repo access added/removed within an existing
    # installation) has nothing to sync yet — github_installations tracks the
    # installation itself, not per-repo access, so there's no row-level change
    # to make here today. Accepted (200) so GitHub doesn't retry.
    elif event in _INGESTED_EVENT_TYPES:
        _handle_ingested_event(db, event, delivery_id, raw_body, payload)

    return {"ok": True}


def _resolve_event_installation_id(payload: dict) -> int | None:
    installation_id = (payload.get("installation") or {}).get("id")
    if not isinstance(installation_id, int) or isinstance(installation_id, bool):
        return None
    return installation_id


def _handle_ingested_event(db: Session, event: str, delivery_id: str, raw_body: bytes, payload: dict) -> None:
    installation_id = _resolve_event_installation_id(payload)

    tenant_id = None
    if installation_id is not None:
        # SECURITY DEFINER lookup (migration 0035), same as installation_repo's
        # delete-under-RLS fix (issue #191/S3 PR 2) -- this receiver never has an
        # authenticated session, so it never has app.tenant_id set to look this up
        # the normal way.
        tenant_id = db.execute(
            text("SELECT resolve_installation_tenant_id(:installation_id)"), {"installation_id": installation_id}
        ).scalar()

    row = WebhookDelivery(
        tenant_id=tenant_id,
        delivery_id=delivery_id,
        event_type=event,
        installation_id=installation_id,
        payload=raw_body,
        status="queued",
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    try:
        client = get_redis_client()
        client.xadd(
            _WEBHOOK_STREAM_KEY,
            {
                "delivery_row_id": row.id,
                "event_type": event,
                "tenant_id": tenant_id if tenant_id is not None else "",
            },
        )
    except Exception:
        # A transient Redis blip isn't GitHub's retry problem to solve -- the payload
        # is already durably stored above. Mark it so a future re-enqueue sweep (out
        # of scope here) can find it; still a 200 to GitHub either way.
        logger.exception("Failed to enqueue webhook_deliveries row %s onto Redis stream %s", row.id, _WEBHOOK_STREAM_KEY)
        row.status = "queue_failed"
        db.commit()


def _handle_installation_deleted(db: Session, payload: dict) -> None:
    installation_id = (payload.get("installation") or {}).get("id")
    if not isinstance(installation_id, int) or isinstance(installation_id, bool):
        logger.warning("installation.deleted webhook has a missing/non-integer installation.id: %r", installation_id)
        return
    removed, tenant_id = installation_repo.delete_by_installation_id(db, installation_id)
    if removed:
        audit_repo.write(
            db,
            actor="github-webhook",
            action="installation.deleted",
            target=str(installation_id),
            payload={"installation_id": installation_id, "rows_removed": removed},
            tenant_id=tenant_id,
        )
