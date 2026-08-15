"""GitHub App webhook receiver.

  POST /webhooks/github   verifies X-Hub-Signature-256, then handles installation
                           lifecycle events to keep github_installations in sync.
"""

import hashlib
import hmac
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from src.core.config import settings
from src.core.db import get_db
from src.core.rate_limit import rate_limit
from src.repositories import audit_repo, installation_repo

logger = logging.getLogger(__name__)

router = APIRouter()

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

    return {"ok": True}


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
