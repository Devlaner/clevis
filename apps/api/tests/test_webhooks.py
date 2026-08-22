"""Tests for the GitHub App webhook receiver."""

import hashlib
import hmac
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from src.core.config import settings
from src.core.db import AuditLog, WebhookDelivery, get_db
from src.core.redis_client import get_redis_client
from src.repositories import installation_repo, org_repo
from src.routers import webhooks as webhooks_module
from src.routers.webhooks import _WEBHOOK_STREAM_KEY, router as webhooks_router

_SECRET = "test-webhook-secret"


@pytest.fixture()
def webhook_client(db, monkeypatch):
    monkeypatch.setattr(settings, "github_app_webhook_secret", SecretStr(_SECRET))
    app = FastAPI()
    app.include_router(webhooks_router)
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


@pytest.fixture()
def redis_client():
    # FLUSHDB, not a per-test key prefix: the ingestion path's stream key is a fixed
    # module constant, not parameterized per test, so isolating one test's entries
    # from another's needs the whole (test) DB cleared -- fine since this is a
    # dedicated Redis instance for the test suite, never a shared/production one.
    client = get_redis_client()
    client.flushdb()
    yield client
    client.flushdb()


def _sign(body: bytes, secret: str = _SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _post(
    client,
    event: str,
    payload: dict,
    *,
    secret: str = _SECRET,
    signature: str | None = None,
    headers_extra: dict | None = None,
):
    body = json.dumps(payload).encode()
    sig = signature if signature is not None else _sign(body, secret)
    headers = {
        "Content-Type": "application/json",
        "X-GitHub-Event": event,
        "X-Hub-Signature-256": sig,
    }
    if headers_extra:
        headers.update(headers_extra)
    return client.post("/webhooks/github", content=body, headers=headers)


def test_rejects_missing_signature(webhook_client):
    resp = webhook_client.post(
        "/webhooks/github",
        content=b'{"action": "deleted"}',
        headers={"X-GitHub-Event": "installation"},
    )
    assert resp.status_code == 401


def test_rejects_invalid_signature(webhook_client):
    resp = _post(webhook_client, "installation", {"action": "deleted"}, signature="sha256=deadbeef")
    assert resp.status_code == 401


def test_rejects_signature_signed_with_wrong_secret(webhook_client):
    resp = _post(webhook_client, "installation", {"action": "deleted"}, secret="wrong-secret")
    assert resp.status_code == 401


def test_returns_503_when_webhook_secret_not_configured(db, monkeypatch):
    monkeypatch.setattr(settings, "github_app_webhook_secret", None)
    app = FastAPI()
    app.include_router(webhooks_router)
    app.dependency_overrides[get_db] = lambda: db
    client = TestClient(app)
    resp = _post(client, "installation", {"action": "deleted"})
    assert resp.status_code == 503


def test_installation_deleted_removes_matching_rows_and_writes_audit_log(db, webhook_client):
    org = org_repo.get_or_create(db, github_login="acme")
    installation_repo.create(
        db, account_login="acme", account_type="Organization", auth_mode="app", installation_id=42, org_id=org.id
    )

    resp = _post(webhook_client, "installation", {"action": "deleted", "installation": {"id": 42}})

    assert resp.status_code == 200
    assert installation_repo.list_for_org(db, org_id=org.id) == []
    logs = db.query(AuditLog).filter(AuditLog.action == "installation.deleted").all()
    assert len(logs) == 1
    assert logs[0].actor == "github-webhook"
    assert logs[0].target == "42"


def test_installation_deleted_only_removes_matching_installation_id(db, webhook_client):
    org = org_repo.get_or_create(db, github_login="acme")
    installation_repo.create(
        db, account_login="acme", account_type="Organization", auth_mode="app", installation_id=42, org_id=org.id
    )

    resp = _post(webhook_client, "installation", {"action": "deleted", "installation": {"id": 999}})

    assert resp.status_code == 200
    assert len(installation_repo.list_for_org(db, org_id=org.id)) == 1


def test_installation_created_action_is_a_no_op(db, webhook_client):
    org = org_repo.get_or_create(db, github_login="acme")
    installation_repo.create(
        db, account_login="acme", account_type="Organization", auth_mode="app", installation_id=42, org_id=org.id
    )

    resp = _post(webhook_client, "installation", {"action": "created", "installation": {"id": 42}})

    assert resp.status_code == 200
    assert len(installation_repo.list_for_org(db, org_id=org.id)) == 1


def test_unrecognized_event_type_returns_200_without_side_effects(webhook_client):
    resp = _post(webhook_client, "ping", {"zen": "hello"})
    assert resp.status_code == 200


def test_malformed_json_body_returns_400(webhook_client):
    body = b"not json"
    resp = webhook_client.post(
        "/webhooks/github",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Event": "installation",
            "X-Hub-Signature-256": _sign(body),
        },
    )
    assert resp.status_code == 400


def test_non_ascii_signature_header_is_a_clean_401_not_a_crash(webhook_client):
    # hmac.compare_digest raises TypeError on a `str` with non-ASCII characters.
    # HTTP header values are latin-1, so a raw byte like \xe9 is a valid header on
    # the wire but decodes to a non-ASCII Python str — pass raw bytes (not a str,
    # which httpx would refuse to even encode as a header) to actually exercise
    # that path. Must fail closed with 401, not bubble up as an unhandled 500.
    body = json.dumps({"action": "deleted"}).encode()
    resp = webhook_client.post(
        "/webhooks/github",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Event": "installation",
            "X-Hub-Signature-256": b"sha256=\xe9\xe9\xe9\xe9",
        },
    )
    assert resp.status_code == 401


def test_oversized_body_rejected_with_413(webhook_client):
    from src.routers.webhooks import _MAX_BODY_BYTES

    oversized = b"a" * (_MAX_BODY_BYTES + 1)
    resp = webhook_client.post(
        "/webhooks/github",
        content=oversized,
        headers={
            "Content-Type": "application/octet-stream",
            "X-GitHub-Event": "installation",
            "X-Hub-Signature-256": _sign(oversized),
        },
    )
    assert resp.status_code == 413


def test_installation_deleted_ignores_non_integer_installation_id(db, webhook_client):
    org = org_repo.get_or_create(db, github_login="acme")
    installation_repo.create(
        db, account_login="acme", account_type="Organization", auth_mode="app", installation_id=42, org_id=org.id
    )

    resp = _post(webhook_client, "installation", {"action": "deleted", "installation": {"id": "not-an-int"}})

    assert resp.status_code == 200
    # Nothing should be deleted, and no exception should propagate from a type mismatch
    # hitting the database — an installation_id of the wrong type must be a safe no-op.
    assert len(installation_repo.list_for_org(db, org_id=org.id)) == 1


def test_push_event_writes_webhook_delivery_row_and_queues_it(db, webhook_client, redis_client):
    org = org_repo.get_or_create(db, github_login="acme")
    installation_repo.create(
        db, account_login="acme", account_type="Organization", auth_mode="app", installation_id=42, org_id=org.id
    )

    resp = _post(
        webhook_client,
        "push",
        {"installation": {"id": 42}, "ref": "refs/heads/main"},
        headers_extra={"X-GitHub-Delivery": "delivery-abc-123"},
    )

    assert resp.status_code == 200
    rows = db.query(WebhookDelivery).filter(WebhookDelivery.delivery_id == "delivery-abc-123").all()
    assert len(rows) == 1
    row = rows[0]
    assert row.event_type == "push"
    assert row.installation_id == 42
    assert row.status == "queued"
    assert row.tenant_id is not None
    assert json.loads(row.payload) == {"installation": {"id": 42}, "ref": "refs/heads/main"}

    entries = redis_client.xrange(_WEBHOOK_STREAM_KEY)
    assert len(entries) == 1
    _, fields = entries[0]
    assert fields["event_type"] == "push"
    assert fields["delivery_row_id"] == str(row.id)
    assert fields["tenant_id"] == str(row.tenant_id)


def test_ingested_event_without_installation_still_queues_with_null_tenant(db, webhook_client, redis_client):
    resp = _post(webhook_client, "issues", {"action": "opened"}, headers_extra={"X-GitHub-Delivery": "delivery-no-inst"})

    assert resp.status_code == 200
    row = db.query(WebhookDelivery).filter(WebhookDelivery.delivery_id == "delivery-no-inst").first()
    assert row is not None
    assert row.installation_id is None
    assert row.tenant_id is None
    assert row.status == "queued"

    entries = redis_client.xrange(_WEBHOOK_STREAM_KEY)
    assert len(entries) == 1
    assert entries[0][1]["tenant_id"] == ""


@pytest.mark.parametrize("event", ["dependabot_alert", "code_scanning_alert", "secret_scanning_alert"])
def test_security_alert_event_writes_webhook_delivery_row_and_queues_it(event, db, webhook_client, redis_client):
    # No consumer normalizes these into per-repo alert tables yet (a later PR) -- this
    # only proves the receiver durably lands them via the same generic path push/issues
    # already use, same as test_push_event_writes_webhook_delivery_row_and_queues_it.
    org = org_repo.get_or_create(db, github_login="acme")
    installation_repo.create(
        db, account_login="acme", account_type="Organization", auth_mode="app", installation_id=42, org_id=org.id
    )

    resp = _post(
        webhook_client,
        event,
        {"installation": {"id": 42}, "action": "created"},
        headers_extra={"X-GitHub-Delivery": f"delivery-{event}"},
    )

    assert resp.status_code == 200
    row = db.query(WebhookDelivery).filter(WebhookDelivery.delivery_id == f"delivery-{event}").first()
    assert row is not None
    assert row.event_type == event
    assert row.installation_id == 42
    assert row.tenant_id is not None
    assert row.status == "queued"

    entries = redis_client.xrange(_WEBHOOK_STREAM_KEY)
    assert len(entries) == 1
    assert entries[0][1]["event_type"] == event


@pytest.mark.parametrize("event", ["member", "organization", "membership", "team"])
def test_collaborators_event_writes_webhook_delivery_row_and_queues_it(event, db, webhook_client, redis_client):
    # Same generic-receiver proof as the security-alert parametrized test above --
    # member/organization are normalized by event_consumer.py (post-S6 Collaborators PR 1);
    # membership/team are durably queued but acked-and-skipped for now (team-based repo
    # access is deferred, see event_consumer.py's docstring).
    org = org_repo.get_or_create(db, github_login="acme")
    installation_repo.create(
        db, account_login="acme", account_type="Organization", auth_mode="app", installation_id=42, org_id=org.id
    )

    resp = _post(
        webhook_client,
        event,
        {"installation": {"id": 42}, "action": "added"},
        headers_extra={"X-GitHub-Delivery": f"delivery-{event}"},
    )

    assert resp.status_code == 200
    row = db.query(WebhookDelivery).filter(WebhookDelivery.delivery_id == f"delivery-{event}").first()
    assert row is not None
    assert row.event_type == event
    assert row.installation_id == 42
    assert row.tenant_id is not None
    assert row.status == "queued"

    entries = redis_client.xrange(_WEBHOOK_STREAM_KEY)
    assert len(entries) == 1
    assert entries[0][1]["event_type"] == event


def test_unrecognized_event_type_does_not_write_a_webhook_delivery_row(db, webhook_client, redis_client):
    resp = _post(webhook_client, "ping", {"zen": "hello"})

    assert resp.status_code == 200
    assert db.query(WebhookDelivery).count() == 0
    assert redis_client.xrange(_WEBHOOK_STREAM_KEY) == []


def test_redis_unreachable_still_returns_200_and_marks_row_queue_failed(db, webhook_client, monkeypatch):
    class _BrokenClient:
        def xadd(self, *args, **kwargs):
            raise ConnectionError("redis unreachable")

    monkeypatch.setattr(webhooks_module, "get_redis_client", lambda: _BrokenClient())

    resp = _post(
        webhook_client, "release", {"action": "published"}, headers_extra={"X-GitHub-Delivery": "delivery-redis-down"}
    )

    assert resp.status_code == 200
    row = db.query(WebhookDelivery).filter(WebhookDelivery.delivery_id == "delivery-redis-down").first()
    assert row is not None
    assert row.status == "queue_failed"


def test_route_is_registered_on_the_real_app(db, monkeypatch):
    # Every other test in this file mounts `webhooks_router` on an isolated FastAPI()
    # instance, so they would all still pass even if `src.main` never actually included
    # the router — a dropped `app.include_router(webhooks.router, ...)` in main.py would
    # only surface as a 404 in production. Import the real app the way
    # `uvicorn src.main:app` does and hit the route through it, so a missing/broken
    # registration fails here instead.
    from src.main import app as real_app

    monkeypatch.setattr(settings, "github_app_webhook_secret", SecretStr(_SECRET))
    real_app.dependency_overrides[get_db] = lambda: db
    try:
        resp = _post(TestClient(real_app), "ping", {"zen": "hello"})
    finally:
        real_app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
