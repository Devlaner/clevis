"""Tests for Dependabot auto-triage (issue #290).

Org-admin only. Per-repo opt-in (default off); approve_only unless approve_and_merge is
set. A PR is acted on only when it's a patch-level dependabot[bot] bump with all checks
green and no pending/blocking human review. Every decision is audited. Faked GitHub via
``patch("src.routers.dependabot_triage.GitHubClient")``.
"""

from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.core.auth import UserOut, require_auth
from src.core.db import AuditLog, User, get_db
from src.repositories import automation_settings_repo, org_membership_repo, org_repo
from src.routers.dependabot_triage import router
from src.services import dependabot_triage
from src.services.dependabot_triage import Decision, triage


def _pr(number=1, *, login="dependabot[bot]", body="Bumps foo from 1.2.3 to 1.2.4.", draft=False,
        sha="headsha", requested_reviewers=None):
    return {
        "number": number,
        "title": f"Bump foo to 1.2.{number}",
        "user": {"login": login},
        "body": body,
        "draft": draft,
        "head": {"sha": sha},
        "requested_reviewers": requested_reviewers or [],
        "requested_teams": [],
    }


def _wire(mock, *, prs, check_runs=None, status_state="success", statuses=None, reviews=None):
    inst = mock.return_value
    calls = {"reviews_posted": [], "merges": []}
    check_runs = check_runs if check_runs is not None else [{"status": "completed", "conclusion": "success"}]

    def request(method, path, params=None, json=None):
        if path.endswith("/pulls") and method == "GET":
            return prs
        if path.endswith("/check-runs"):
            return {"check_runs": check_runs}
        if path.endswith("/status"):
            return {"state": status_state, "statuses": statuses if statuses is not None else []}
        if path.endswith("/reviews") and method == "GET":
            return reviews or []
        if path.endswith("/reviews") and method == "POST":
            calls["reviews_posted"].append(path)
            return {"id": 1}
        if path.endswith("/merge") and method == "PUT":
            calls["merges"].append((path, json))
            return {"merged": True}
        return {}

    inst.request.side_effect = request
    return inst, calls


# --- service: eligibility ------------------------------------------------


_GREEN_RUNS = {"check_runs": [{"status": "completed", "conclusion": "success"}]}
_GREEN_STATUS = {"state": "success", "statuses": []}


class _FakeClient:
    """Endpoint-aware fake. Order of the checks below matters — the more specific
    suffixes are tested before ``/pulls``."""

    def __init__(self, *, prs, check_runs=_GREEN_RUNS, status=_GREEN_STATUS, reviews=None):
        self.prs = prs
        self.check_runs = check_runs
        self.status = status
        self.reviews = reviews or []
        self.calls = []

    def request(self, method, path, params=None, json=None):
        self.calls.append((method, path, json))
        if path.endswith("/check-runs"):
            return self.check_runs
        if path.endswith("/status"):
            return self.status
        if path.endswith("/reviews"):
            return {"id": 1} if method == "POST" else self.reviews
        if path.endswith("/merge"):
            return {"merged": True}
        if path.endswith("/pulls"):
            return self.prs
        return {}


def _run(prs, *, mode="approve_only", dry_run=False, check_runs=_GREEN_RUNS, status=_GREEN_STATUS, reviews=None):
    client = _FakeClient(prs=prs, check_runs=check_runs, status=status, reviews=reviews)
    return client, triage(client, "acme", "api", enabled=True, mode=mode, dry_run=dry_run)


def test_disabled_repo_does_nothing():
    client = _FakeClient(prs=[_pr()])
    assert triage(client, "acme", "api", enabled=False, mode="approve_only") == []
    assert client.calls == []


def test_non_dependabot_pr_is_skipped():
    _c, decisions = _run([_pr(login="alice")])
    assert decisions[0].action == "skipped" and "Dependabot" in decisions[0].reason


def test_non_patch_bump_is_skipped():
    _c, decisions = _run([_pr(body="Bumps foo from 1.2.3 to 1.3.0.")])
    assert decisions[0].reason == "not a patch-level bump"


def test_unparseable_bump_body_is_skipped():
    _c, decisions = _run([_pr(body="Update foo, see changelog")])
    assert "could not determine" in decisions[0].reason


def test_draft_pr_is_skipped():
    _c, decisions = _run([_pr(draft=True)])
    assert decisions[0].reason == "draft PR"


def test_failing_check_run_is_skipped():
    _c, decisions = _run([_pr()], check_runs={"check_runs": [{"status": "completed", "conclusion": "failure"}]})
    assert decisions[0].reason == "checks are not all green"


def test_pending_check_run_is_skipped():
    _c, decisions = _run(
        [_pr()],
        check_runs={"check_runs": [{"status": "in_progress", "conclusion": None}]},
        status={"state": "pending", "statuses": [{"context": "ci"}]},
    )
    assert decisions[0].reason == "checks are not all green"


def test_failing_classic_status_is_skipped():
    _c, decisions = _run(
        [_pr()],
        check_runs={"check_runs": []},
        status={"state": "failure", "statuses": [{"context": "ci/jenkins"}]},
    )
    assert decisions[0].reason == "checks are not all green"


def test_head_sha_with_no_ci_at_all_is_skipped():
    _c, decisions = _run([_pr()], check_runs={"check_runs": []}, status={"state": "pending", "statuses": []})
    assert decisions[0].reason == "checks are not all green"


def test_green_via_classic_status_only_is_eligible():
    _c, decisions = _run(
        [_pr()],
        check_runs={"check_runs": []},
        status={"state": "success", "statuses": [{"context": "ci/jenkins", "state": "success"}]},
    )
    assert decisions[0].action == "approved"


def test_changes_requested_review_is_skipped():
    _c, decisions = _run([_pr()], reviews=[{"state": "CHANGES_REQUESTED"}])
    assert "human review" in decisions[0].reason


def test_requested_reviewer_still_pending_is_skipped():
    _c, decisions = _run([_pr(requested_reviewers=[{"login": "carol"}])])
    assert "human review" in decisions[0].reason


# --- service: actions --------------------------------------------------


def test_approve_only_approves_and_never_merges():
    client, decisions = _run([_pr()], mode="approve_only")
    assert decisions[0].action == "approved"
    assert any("/reviews" in c[1] and c[0] == "POST" for c in client.calls)
    assert not any("/merge" in c[1] for c in client.calls)


def test_approve_and_merge_approves_then_merges():
    client, decisions = _run([_pr()], mode="approve_and_merge")
    assert decisions[0].action == "merged"
    review_i = next(i for i, c in enumerate(client.calls) if "/reviews" in c[1] and c[0] == "POST")
    merge_i = next(i for i, c in enumerate(client.calls) if "/merge" in c[1])
    assert review_i < merge_i


def test_dry_run_makes_no_write_calls():
    client, decisions = _run([_pr()], mode="approve_and_merge", dry_run=True)
    assert decisions[0].action == "would_merge"
    assert not any(c[0] in ("POST", "PUT") for c in client.calls)


def test_per_run_cap_is_respected():
    client = _FakeClient(prs=[_pr(n) for n in range(1, 8)])
    decisions = triage(client, "acme", "api", enabled=True, mode="approve_only", cap=5)
    assert len([d for d in decisions if d.action == "approved"]) == 5
    assert len([d for d in decisions if d.reason == "per-run cap reached"]) == 2


def test_merge_method_from_the_setting_is_used():
    client, _decisions = _run([_pr()], mode="approve_and_merge")
    # default squash here; the router-level test covers a custom method
    merge_call = next(c for c in client.calls if "/merge" in c[1])
    assert merge_call[2] == {"merge_method": "squash"}


# --- router ----------------------------------------------------------


@pytest.fixture()
def acme(db):
    admin = User(email="a@e.com", name=None, password_hash=None, is_workspace_admin=False)
    member = User(email="m@e.com", name=None, password_hash=None, is_workspace_admin=False)
    db.add_all([admin, member])
    db.commit()
    db.refresh(admin)
    db.refresh(member)
    org = org_repo.get_or_create(db, github_login="acme")
    org_membership_repo.get_or_create(db, org_id=org.id, user_id=admin.id, role="admin")
    org_membership_repo.get_or_create(db, org_id=org.id, user_id=member.id, role="member")
    return {"org": org, "admin": admin, "member": member}


def _client(db, user_id, email="a@e.com"):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_auth] = lambda: UserOut(
        id=user_id, email=email, name=None, is_workspace_admin=False
    )
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


def test_setting_endpoint_upserts_and_validates(db, acme):
    client = _client(db, acme["admin"].id)
    bad = client.put(
        "/orgs/acme/repos/acme/api/automation/dependabot-triage",
        json={"enabled": True, "mode": "yolo"},
    )
    assert bad.status_code == 422

    ok = client.put(
        "/orgs/acme/repos/acme/api/automation/dependabot-triage",
        json={"enabled": True, "mode": "approve_and_merge", "merge_method": "rebase"},
    )
    assert ok.status_code == 200
    row = automation_settings_repo.get(db, acme["org"].tenant_id, "acme/api", "dependabot_triage")
    assert row.enabled is True and row.mode == "approve_and_merge"
    assert row.extra["merge_method"] == "rebase"


def test_setting_endpoint_requires_admin(db, acme):
    client = _client(db, acme["member"].id, email="m@e.com")
    resp = client.put(
        "/orgs/acme/repos/acme/api/automation/dependabot-triage",
        json={"enabled": True, "mode": "approve_only"},
    )
    assert resp.status_code == 403


def test_run_skips_repos_that_are_not_enabled(db, acme):
    client = _client(db, acme["admin"].id)
    automation_settings_repo.upsert(
        db, acme["org"].tenant_id, "acme/api", "dependabot_triage", enabled=False, mode="approve_only"
    )
    db.commit()
    with patch("src.routers.dependabot_triage.GitHubClient") as mock:
        _wire(mock, prs=[_pr()])
        resp = client.post("/orgs/acme/dependabot-triage", json={"token": "ghp_admin", "repos": ["acme/api"]})
    assert resp.status_code == 200
    assert resp.json()["decisions"][0]["reason"] == "not enabled for this repo"


def test_run_audits_the_run_and_every_decision(db, acme):
    client = _client(db, acme["admin"].id)
    automation_settings_repo.upsert(
        db, acme["org"].tenant_id, "acme/api", "dependabot_triage", enabled=True, mode="approve_only",
        extra={"merge_method": "squash"},
    )
    db.commit()
    with patch("src.routers.dependabot_triage.GitHubClient") as mock:
        _wire(mock, prs=[_pr(1), _pr(2, login="alice")])
        resp = client.post("/orgs/acme/dependabot-triage", json={"token": "ghp_admin", "repos": ["acme/api"]})
    assert resp.status_code == 200
    actions = {d["number"]: d["action"] for d in resp.json()["decisions"]}
    assert actions == {1: "approved", 2: "skipped"}
    assert db.query(AuditLog).filter(AuditLog.action == "dependabot_triage.run").count() == 1
    assert db.query(AuditLog).filter(AuditLog.action == "dependabot_triage.approved").count() == 1
    assert db.query(AuditLog).filter(AuditLog.action == "dependabot_triage.skipped").count() == 1


def test_run_uses_the_repos_configured_merge_method(db, acme):
    client = _client(db, acme["admin"].id)
    automation_settings_repo.upsert(
        db, acme["org"].tenant_id, "acme/api", "dependabot_triage", enabled=True,
        mode="approve_and_merge", extra={"merge_method": "rebase"},
    )
    db.commit()
    with patch("src.routers.dependabot_triage.GitHubClient") as mock:
        _inst, calls = _wire(mock, prs=[_pr(1)])
        client.post("/orgs/acme/dependabot-triage", json={"token": "ghp_admin", "repos": ["acme/api"]})
    assert calls["merges"] and calls["merges"][0][1] == {"merge_method": "rebase"}


def test_run_requires_admin(db, acme):
    client = _client(db, acme["member"].id, email="m@e.com")
    resp = client.post("/orgs/acme/dependabot-triage", json={"token": "ghp_x"})
    assert resp.status_code == 403


def test_run_no_token_returns_400(db, acme):
    client = _client(db, acme["admin"].id)
    resp = client.post("/orgs/acme/dependabot-triage", json={})
    assert resp.status_code == 400


def test_run_github_403_becomes_400_with_hint(db, acme):
    client = _client(db, acme["admin"].id)
    automation_settings_repo.upsert(
        db, acme["org"].tenant_id, "acme/api", "dependabot_triage", enabled=True, mode="approve_only"
    )
    db.commit()
    err = httpx.HTTPStatusError(
        "403", request=httpx.Request("GET", "https://api.github.com"), response=httpx.Response(403)
    )
    with patch("src.routers.dependabot_triage.GitHubClient") as mock:
        mock.return_value.request.side_effect = err
        resp = client.post("/orgs/acme/dependabot-triage", json={"token": "ghp_x", "repos": ["acme/api"]})
    assert resp.status_code == 400 and "Pull requests" in resp.json()["detail"]


def test_setting_endpoint_rejects_a_bad_merge_method(db, acme):
    client = _client(db, acme["admin"].id)
    resp = client.put(
        "/orgs/acme/repos/acme/api/automation/dependabot-triage",
        json={"enabled": True, "mode": "approve_only", "merge_method": "fast-forward"},
    )
    assert resp.status_code == 422


def test_run_accepts_a_bare_repo_name(db, acme):
    client = _client(db, acme["admin"].id)
    automation_settings_repo.upsert(
        db, acme["org"].tenant_id, "acme/api", "dependabot_triage", enabled=True, mode="approve_only"
    )
    db.commit()
    with patch("src.routers.dependabot_triage.GitHubClient") as mock:
        _wire(mock, prs=[_pr(1)])
        resp = client.post("/orgs/acme/dependabot-triage", json={"token": "ghp_admin", "repos": ["api"]})
    assert resp.status_code == 200
    assert resp.json()["decisions"][0]["action"] == "approved"


def test_run_non_403_github_error_is_surfaced(db, acme):
    client = _client(db, acme["admin"].id)
    automation_settings_repo.upsert(
        db, acme["org"].tenant_id, "acme/api", "dependabot_triage", enabled=True, mode="approve_only"
    )
    db.commit()
    err = httpx.HTTPStatusError(
        "500", request=httpx.Request("GET", "https://api.github.com"), response=httpx.Response(500)
    )
    with patch("src.routers.dependabot_triage.GitHubClient") as mock:
        mock.return_value.request.side_effect = err
        resp = client.post("/orgs/acme/dependabot-triage", json={"token": "ghp_x", "repos": ["acme/api"]})
    assert resp.status_code >= 400 and "Pull requests" not in resp.json()["detail"]


def test_run_surfaces_a_network_error(db, acme):
    client = _client(db, acme["admin"].id)
    automation_settings_repo.upsert(
        db, acme["org"].tenant_id, "acme/api", "dependabot_triage", enabled=True, mode="approve_only"
    )
    db.commit()
    with patch("src.routers.dependabot_triage.GitHubClient") as mock:
        mock.return_value.request.side_effect = httpx.ConnectError("boom")
        resp = client.post("/orgs/acme/dependabot-triage", json={"token": "ghp_x", "repos": ["acme/api"]})
    assert resp.status_code >= 500


def test_run_dry_run_writes_no_reviews(db, acme):
    client = _client(db, acme["admin"].id)
    automation_settings_repo.upsert(
        db, acme["org"].tenant_id, "acme/api", "dependabot_triage", enabled=True, mode="approve_and_merge"
    )
    db.commit()
    with patch("src.routers.dependabot_triage.GitHubClient") as mock:
        _inst, calls = _wire(mock, prs=[_pr(1)])
        resp = client.post(
            "/orgs/acme/dependabot-triage",
            json={"token": "ghp_admin", "repos": ["acme/api"], "dry_run": True},
        )
    assert resp.json()["decisions"][0]["action"] == "would_merge"
    assert not calls["reviews_posted"] and not calls["merges"]
