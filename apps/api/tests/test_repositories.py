from src.repositories import audit_repo, installation_repo, job_repo, org_repo


def test_installation_create(db):
    org = org_repo.get_or_create(db, github_login="acme")
    row = installation_repo.create(
        db,
        account_login="acme",
        account_type="Organization",
        auth_mode="app",
        installation_id=42,
        org_id=org.id,
    )
    assert row.id is not None
    assert row.token_ref == "tok_acme"
    assert row.account_login == "acme"


def test_audit_write(db):
    org = org_repo.get_or_create(db, github_login="acme")
    audit_repo.write(db, actor="bot", action="test.event", target="acme/repo", payload={"x": 1}, tenant_id=org.tenant_id)
    # no exception means the row was inserted and committed via savepoint


def test_job_enqueue_and_list(db):
    job_id = job_repo.enqueue(db, "github.clear_actions_cache", {"owner": "acme", "repo": "api"})
    assert isinstance(job_id, int)

    jobs = job_repo.list_jobs(db)
    assert any(j["id"] == job_id for j in jobs)
    match = next(j for j in jobs if j["id"] == job_id)
    assert match["status"] == "queued"
    assert match["job_type"] == "github.clear_actions_cache"


def test_job_mark_done(db):
    job_id = job_repo.enqueue(db, "github.clear_actions_cache", {"owner": "acme", "repo": "api"})
    job_repo.mark_done(db, job_id, '{"ok": true}')
    jobs = job_repo.list_jobs(db)
    match = next(j for j in jobs if j["id"] == job_id)
    assert match["status"] == "done"


def test_job_mark_failed(db):
    job_id = job_repo.enqueue(db, "github.clear_actions_cache", {"owner": "acme", "repo": "api"})
    job_repo.mark_failed(db, job_id, "timeout")
    jobs = job_repo.list_jobs(db)
    match = next(j for j in jobs if j["id"] == job_id)
    assert match["status"] == "failed"
    assert match["result"] == "timeout"


def test_job_mark_failed_truncates_long_error(db):
    job_id = job_repo.enqueue(db, "github.clear_actions_cache", {"owner": "acme", "repo": "api"})
    job_repo.mark_failed(db, job_id, "x" * 1000)
    jobs = job_repo.list_jobs(db)
    match = next(j for j in jobs if j["id"] == job_id)
    assert len(match["result"]) <= 500
    assert match["result"].endswith("...(truncated)")


def test_job_mark_failed_redacts_token_shaped_text(db):
    job_id = job_repo.enqueue(db, "github.clear_actions_cache", {"owner": "acme", "repo": "api"})
    job_repo.mark_failed(db, job_id, "failed with ghp_abcdefghijklmnopqrstuvwxyz0123456789")
    jobs = job_repo.list_jobs(db)
    match = next(j for j in jobs if j["id"] == job_id)
    assert "ghp_" not in match["result"]
    assert "[redacted]" in match["result"]
