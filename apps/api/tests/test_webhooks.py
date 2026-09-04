"""Tests for the GitHub App webhook receiver."""

import hashlib
import hmac
import json
import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import text

from src.core.config import settings
from src.core.db import AuditLog, GitHubInstallation, SessionLocal, WebhookDelivery, get_db, set_session_tenant
from src.core.rbac import set_tenant_session_context
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


def test_installation_new_permissions_accepted_persists_permissions_and_audits(db, webhook_client):
    org = org_repo.get_or_create(db, github_login="acme")
    installation_repo.create(
        db, account_login="acme", account_type="Organization", auth_mode="app", installation_id=77, org_id=org.id
    )

    perms = {"issues": "write", "contents": "read", "metadata": "read"}
    resp = _post(
        webhook_client,
        "installation",
        {"action": "new_permissions_accepted", "installation": {"id": 77, "permissions": perms}},
    )

    assert resp.status_code == 200
    row = installation_repo.list_for_org(db, org_id=org.id)[0]
    assert row.granted_permissions == perms
    assert row.permissions_synced_at is not None

    logs = db.query(AuditLog).filter(AuditLog.action == "installation.permissions_accepted").all()
    assert len(logs) == 1
    assert logs[0].actor == "github-webhook"
    assert logs[0].target == "77"


def test_installation_new_permissions_accepted_redelivery_does_not_duplicate_audit_row(db, webhook_client):
    # GitHub redelivers webhooks on retry (and a redelivery can be triggered manually
    # from the GitHub UI) -- the same new_permissions_accepted delivery landing twice
    # must not write a second, duplicate audit entry for what is really one approval.
    org = org_repo.get_or_create(db, github_login="acme")
    installation_repo.create(
        db, account_login="acme", account_type="Organization", auth_mode="app", installation_id=77, org_id=org.id
    )
    perms = {"issues": "write", "contents": "read", "metadata": "read"}
    payload = {"action": "new_permissions_accepted", "installation": {"id": 77, "permissions": perms}}

    first = _post(webhook_client, "installation", payload)
    second = _post(webhook_client, "installation", payload)

    assert first.status_code == 200
    assert second.status_code == 200
    logs = db.query(AuditLog).filter(AuditLog.action == "installation.permissions_accepted").all()
    assert len(logs) == 1
    # permissions_synced_at still reflects the redelivery even though nothing changed.
    row = installation_repo.list_for_org(db, org_id=org.id)[0]
    assert row.granted_permissions == perms


def test_update_permissions_serializes_concurrent_redeliveries():
    """CodeRabbit finding on #400: the compare-then-update in
    installation_repo.update_permissions wasn't atomic -- two genuinely concurrent
    new_permissions_accepted deliveries for the same installation could both read the
    same pre-update permissions, both compute changed=True, and both tell the webhook
    handler to write an audit row. Needs two real separate connections (not the
    savepoint-per-test `db` fixture -- same reasoning as
    test_org_membership_repo.py's own concurrency test), since the race only exists
    across genuinely concurrent transactions.

    Session A manually takes the same FOR UPDATE lock update_permissions takes
    internally and holds it open, simulating "another delivery is mid-update" right as
    session B's real update_permissions call starts. B must block until A commits, then
    -- since A already wrote the exact permissions B is about to write -- B's own
    comparison must see no change.
    """
    setup = SessionLocal()
    try:
        org = org_repo.get_or_create(setup, github_login="acme-lock-test")
        # A genuinely separate SessionLocal() commits for real (unlike the savepoint-per-test
        # `db` fixture, where a nested db.commit() never actually ends the outer transaction,
        # so a SET LOCAL from an earlier commit -- ensure_tenant_linked's own -- stays in
        # effect for the rest of the test almost by accident). Real inserts need the same
        # explicit tenant context the router itself sets before writing a row.
        set_tenant_session_context(setup, org.tenant_id, 0)
        # A direct insert, not installation_repo.create()/upsert() -- that helper's own
        # db.commit() + db.refresh() immediately after can itself hit issue #330's documented
        # nested-commit/RLS-timing flakiness under genuine thread concurrency (same class as
        # test_org_membership_repo.py's xfail'd tests), which is no part of what this test
        # means to exercise. Nothing after this needs the row's server-generated defaults.
        setup.add(
            GitHubInstallation(
                account_login="acme-lock-test", account_type="Organization", auth_mode="app",
                installation_id=9001, token_ref="tok_acme-lock-test", org_id=org.id, tenant_id=org.tenant_id,
            )
        )
        setup.commit()
    finally:
        setup.close()

    perms = {"issues": "write", "contents": "read", "metadata": "read"}

    session_a = SessionLocal()
    lock_held = threading.Event()
    release_lock = threading.Event()
    b_result: dict[str, tuple[int, bool]] = {}

    def hold_lock_in_session_a():
        # Resolve/set tenant context exactly as update_permissions itself does -- under
        # RLS (the CI job's constrained clevis_api role), a raw SELECT without this first
        # would see zero rows and take no lock at all, making the test pass for the wrong
        # reason (or flake) instead of genuinely exercising the row lock.
        tenant_id = session_a.execute(
            text("SELECT resolve_installation_tenant_id(:iid)"), {"iid": 9001}
        ).scalar()
        set_session_tenant(session_a, tenant_id)
        session_a.execute(
            text("SELECT * FROM github_installations WHERE installation_id = :iid FOR UPDATE"),
            {"iid": 9001},
        )
        lock_held.set()
        assert release_lock.wait(timeout=5), "test never released session A's lock"
        session_a.query(GitHubInstallation).filter(GitHubInstallation.installation_id == 9001).update(
            {GitHubInstallation.granted_permissions: perms}
        )
        session_a.commit()

    def run_update_permissions_in_session_b():
        session_b = SessionLocal()
        try:
            b_result["value"] = installation_repo.update_permissions(
                session_b, installation_id=9001, permissions=perms
            )
        finally:
            session_b.close()

    try:
        lock_thread = threading.Thread(target=hold_lock_in_session_a)
        lock_thread.start()
        assert lock_held.wait(timeout=5), "session A never acquired its lock"

        b_thread = threading.Thread(target=run_update_permissions_in_session_b)
        b_thread.start()
        b_thread.join(timeout=0.3)
        assert not b_result, "update_permissions must block while another delivery holds the row lock"

        release_lock.set()
        lock_thread.join(timeout=5)
        b_thread.join(timeout=5)

        assert b_result.get("value") == (1, False)
    finally:
        session_a.close()


def test_installation_new_permissions_accepted_writes_new_audit_row_when_permissions_actually_change(db, webhook_client):
    org = org_repo.get_or_create(db, github_login="acme")
    installation_repo.create(
        db, account_login="acme", account_type="Organization", auth_mode="app", installation_id=77, org_id=org.id
    )

    _post(
        webhook_client,
        "installation",
        {"action": "new_permissions_accepted", "installation": {"id": 77, "permissions": {"issues": "write"}}},
    )
    _post(
        webhook_client,
        "installation",
        {
            "action": "new_permissions_accepted",
            "installation": {"id": 77, "permissions": {"issues": "write", "pull_requests": "write"}},
        },
    )

    logs = db.query(AuditLog).filter(AuditLog.action == "installation.permissions_accepted").all()
    assert len(logs) == 2


def test_installation_new_permissions_accepted_unknown_install_is_a_noop(db, webhook_client):
    resp = _post(
        webhook_client,
        "installation",
        {"action": "new_permissions_accepted", "installation": {"id": 12345, "permissions": {"issues": "write"}}},
    )
    assert resp.status_code == 200
    assert db.query(AuditLog).filter(AuditLog.action == "installation.permissions_accepted").count() == 0


def test_installation_suspend_and_unsuspend_return_200_without_side_effects(db, webhook_client):
    org = org_repo.get_or_create(db, github_login="acme")
    installation_repo.create(
        db, account_login="acme", account_type="Organization", auth_mode="app", installation_id=88, org_id=org.id
    )
    for action in ("suspend", "unsuspend"):
        resp = _post(webhook_client, "installation", {"action": action, "installation": {"id": 88}})
        assert resp.status_code == 200
    assert len(installation_repo.list_for_org(db, org_id=org.id)) == 1


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
    # These are normalized by event_consumer.py into security_alerts (post-S6 PR 2) -- this
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
