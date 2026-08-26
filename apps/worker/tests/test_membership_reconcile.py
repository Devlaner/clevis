"""Tests for the org-membership reconciliation poll (Collaborators PR 2 of 3): the pure
GitHub-roster fetch (membership_reconcile.py) and the worker.py job handler that writes it
into org_members/repo_collaborators."""

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import httpx
import psycopg
import pytest

import membership_reconcile
import worker
from _crypto import encrypt_job_token
from config import settings

_DB_URL = settings.database_url.get_secret_value().replace("postgresql+psycopg://", "postgresql://")


# ---------------------------------------------------------------------------
# membership_reconcile.py: pure unit tests (no DB, no worker.py job machinery)
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, json_data, links=None, status_code=200, headers=None):
        self._json_data = json_data
        self.links = links or {}
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=MagicMock(), response=self)

    def json(self):
        return self._json_data


class _FakeClient:
    def __init__(self, responses_by_path):
        # {path_substring: [responses...]} -- popped in order per matching path, so each
        # endpoint (members?role=admin, members?role=all, members?filter=2fa_disabled,
        # outside_collaborators) gets its own independent response queue.
        self._responses = {k: list(v) for k, v in responses_by_path.items()}
        self.calls = []

    def get(self, url, headers=None, params=None):
        self.calls.append((url, params))
        for path, queue in self._responses.items():
            if path in url and queue:
                return queue.pop(0)
        raise AssertionError(f"no fake response queued for {url} {params}")


def _member(login, avatar_url="https://example.com/a.png"):
    return {"login": login, "avatar_url": avatar_url}


def test_fetch_org_roster_resolves_role_via_admin_cross_reference():
    client = _FakeClient(
        {
            "/orgs/acme/members": [
                _FakeResponse([_member("owner1")]),  # role=admin (first call)
                _FakeResponse([_member("owner1"), _member("plain-member")]),  # role=all
                _FakeResponse([]),  # filter=2fa_disabled
            ],
            "/orgs/acme/outside_collaborators": [_FakeResponse([])],
        }
    )

    roster = membership_reconcile.fetch_org_roster(client, "https://api.github.com", {}, "acme")

    roles = {m["login"]: m["role"] for m in roster["members"]}
    assert roles == {"owner1": "admin", "plain-member": "member"}
    assert roster["two_factor_disabled_logins"] == set()
    assert roster["outside_logins"] == set()


def test_fetch_org_roster_2fa_overlay_is_none_when_the_call_fails():
    forbidden = _FakeResponse({}, status_code=403)  # no Retry-After/X-RateLimit-Remaining -> genuine 403, no retry
    client = _FakeClient(
        {
            "/orgs/acme/members": [
                _FakeResponse([]),  # role=admin
                _FakeResponse([_member("someone")]),  # role=all
                forbidden,  # filter=2fa_disabled
            ],
            "/orgs/acme/outside_collaborators": [_FakeResponse([])],
        }
    )

    with patch("membership_reconcile.time.sleep"):
        roster = membership_reconcile.fetch_org_roster(client, "https://api.github.com", {}, "acme")

    assert roster["two_factor_disabled_logins"] is None
    assert [m["login"] for m in roster["members"]] == ["someone"]


def test_fetch_org_roster_populates_outside_collaborators():
    client = _FakeClient(
        {
            "/orgs/acme/members": [_FakeResponse([]), _FakeResponse([]), _FakeResponse([])],
            "/orgs/acme/outside_collaborators": [_FakeResponse([_member("contractor")])],
        }
    )

    roster = membership_reconcile.fetch_org_roster(client, "https://api.github.com", {}, "acme")

    assert roster["outside_logins"] == {"contractor"}


def test_get_all_pages_follows_the_link_header():
    page1 = _FakeResponse([_member("a")], links={"next": {"url": "https://api.github.com/orgs/acme/members?page=2"}})
    page2 = _FakeResponse([_member("b")])
    client = _FakeClient({"/orgs/acme/members": [page1, page2]})

    results = membership_reconcile._get_all_pages(client, "https://api.github.com", {}, "/orgs/acme/members", {})

    assert [m["login"] for m in results] == ["a", "b"]


def test_get_all_pages_retries_a_429_and_succeeds():
    rate_limited = _FakeResponse({}, status_code=429)
    success = _FakeResponse([_member("a")])
    client = _FakeClient({"/orgs/acme/members": [rate_limited, success]})

    with patch("membership_reconcile.time.sleep") as mock_sleep:
        results = membership_reconcile._get_all_pages(client, "https://api.github.com", {}, "/orgs/acme/members", {})

    assert [m["login"] for m in results] == ["a"]
    mock_sleep.assert_called_once()


def test_get_all_pages_raises_after_exhausting_retries():
    responses = [_FakeResponse({}, status_code=429) for _ in range(3)]
    client = _FakeClient({"/orgs/acme/members": responses})

    with patch("membership_reconcile.time.sleep"), pytest.raises(httpx.HTTPStatusError):
        membership_reconcile._get_all_pages(client, "https://api.github.com", {}, "/orgs/acme/members", {})


def test_get_all_pages_raises_roster_incomplete_on_a_non_list_page():
    # A malformed/non-list body (e.g. an error object GitHub returned with a 200) must never
    # be silently treated as "zero results" -- that would look identical to a real empty page
    # to reconcile_org_members, which deletes anyone not in the returned set.
    client = _FakeClient({"/orgs/acme/members": [_FakeResponse({"message": "unexpected"})]})

    with pytest.raises(membership_reconcile.RosterIncomplete):
        membership_reconcile._get_all_pages(client, "https://api.github.com", {}, "/orgs/acme/members", {})


def test_get_all_pages_raises_roster_incomplete_when_more_pages_remain_past_max_pages():
    # _MAX_PAGES consecutive pages, each still pointing at a next page -- the loop must not
    # silently stop and return a partial list once it runs out of iterations.
    pages = [
        _FakeResponse([_member(f"m{i}")], links={"next": {"url": f"https://api.github.com/orgs/acme/members?page={i + 1}"}})
        for i in range(membership_reconcile._MAX_PAGES)
    ]
    client = _FakeClient({"/orgs/acme/members": pages})

    with pytest.raises(membership_reconcile.RosterIncomplete):
        membership_reconcile._get_all_pages(client, "https://api.github.com", {}, "/orgs/acme/members", {})


def test_fetch_org_roster_2fa_overlay_is_none_when_incomplete():
    client = _FakeClient(
        {
            "/orgs/acme/members": [
                _FakeResponse([]),  # role=admin
                _FakeResponse([_member("someone")]),  # role=all
                _FakeResponse({"message": "unexpected"}),  # filter=2fa_disabled -- non-list page
            ],
            "/orgs/acme/outside_collaborators": [_FakeResponse([])],
        }
    )

    roster = membership_reconcile.fetch_org_roster(client, "https://api.github.com", {}, "acme")

    assert roster["two_factor_disabled_logins"] is None


def test_fetch_org_roster_propagates_roster_incomplete_for_the_members_call():
    # Unlike the 2FA overlay, an incomplete members/admins/outside_collaborators fetch must
    # propagate -- it isn't best-effort, it's the data reconcile_org_members deletes against.
    client = _FakeClient(
        {
            "/orgs/acme/members": [_FakeResponse({"message": "unexpected"})],
            "/orgs/acme/outside_collaborators": [_FakeResponse([])],
        }
    )

    with pytest.raises(membership_reconcile.RosterIncomplete):
        membership_reconcile.fetch_org_roster(client, "https://api.github.com", {}, "acme")


# ---------------------------------------------------------------------------
# worker._handle_reconcile_org_membership: error paths, mirroring test_backfill.py's
# _FakeConn/_FakeCursor + patch("worker.httpx.Client") convention.
# ---------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self):
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def fetchone(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class _FakeConn:
    def __init__(self):
        self.committed = False
        self.rolled_back = False
        self._cursor = _FakeCursor()

    def cursor(self):
        return self._cursor

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True


def _payload(**kwargs):
    enc = encrypt_job_token("secret", settings.job_secret_key.get_secret_value())
    return json.dumps({"tenant_id": 1, "org_login": "acme", "token": enc, **kwargs})


def test_handler_marks_failed_on_invalid_payload():
    conn = _FakeConn()
    worker._handle_reconcile_org_membership(conn, 1, "{}", 0)
    sql, params = conn._cursor.calls[0]
    assert "status='failed'" in sql


def test_handler_marks_failed_on_undecryptable_token():
    conn = _FakeConn()
    bad_payload = json.dumps({"tenant_id": 1, "org_login": "acme", "token": "not-encrypted"})
    worker._handle_reconcile_org_membership(conn, 1, bad_payload, 0)
    sql, params = conn._cursor.calls[0]
    assert "status='failed'" in sql


def test_handler_marks_failed_on_4xx_from_github():
    conn = _FakeConn()
    with patch("worker.membership_reconcile.fetch_org_roster") as mock_fetch, patch("worker.httpx.Client"):
        mock_response = MagicMock(status_code=404)
        mock_fetch.side_effect = httpx.HTTPStatusError("error", request=MagicMock(), response=mock_response)
        worker._handle_reconcile_org_membership(conn, 2, _payload(), 0)

    sql, params = conn._cursor.calls[0]
    assert "status='failed'" in sql


def test_handler_requeues_on_5xx_from_github():
    conn = _FakeConn()
    with patch("worker.membership_reconcile.fetch_org_roster") as mock_fetch, patch("worker.httpx.Client"):
        mock_response = MagicMock(status_code=502)
        mock_fetch.side_effect = httpx.HTTPStatusError("error", request=MagicMock(), response=mock_response)
        worker._handle_reconcile_org_membership(conn, 3, _payload(), 0)

    sql, params = conn._cursor.calls[0]
    assert "status='queued'" in sql


def test_handler_requeues_on_network_error():
    conn = _FakeConn()
    with patch("worker.membership_reconcile.fetch_org_roster", side_effect=httpx.RequestError("connection reset")), patch(
        "worker.httpx.Client"
    ):
        worker._handle_reconcile_org_membership(conn, 4, _payload(), 0)

    sql, params = conn._cursor.calls[0]
    assert "status='queued'" in sql


def test_handler_requeues_on_roster_incomplete():
    conn = _FakeConn()
    with patch(
        "worker.membership_reconcile.fetch_org_roster",
        side_effect=membership_reconcile.RosterIncomplete("more than _MAX_PAGES pages"),
    ), patch("worker.httpx.Client"):
        worker._handle_reconcile_org_membership(conn, 4, _payload(), 0)

    sql, params = conn._cursor.calls[0]
    assert "status='queued'" in sql


class _FailingCursor(_FakeCursor):
    has_failed = False

    def execute(self, sql, params=None):
        if not self.has_failed:
            self.has_failed = True
            raise psycopg.OperationalError("simulated database failure")
        super().execute(sql, params)


class _FailingConn(_FakeConn):
    def __init__(self):
        super().__init__()
        self._cursor = _FailingCursor()


def test_handler_rolls_back_and_requeues_on_a_db_error():
    conn = _FailingConn()
    roster = {"members": [], "two_factor_disabled_logins": set(), "outside_logins": set()}

    with patch("worker.membership_reconcile.fetch_org_roster", return_value=roster), patch("worker.httpx.Client"):
        worker._handle_reconcile_org_membership(conn, 5, _payload(), 0)

    assert conn.rolled_back is True
    sql, _params = conn._cursor.calls[0]
    assert "status='queued'" in sql
    assert not any("org_membership_sync_cursors" in sql for sql, _params in conn._cursor.calls)


# ---------------------------------------------------------------------------
# worker._handle_reconcile_org_membership: real Postgres, verifying the actual
# org_members / repo_collaborators / org_membership_sync_cursors side effects.
# ---------------------------------------------------------------------------


@pytest.fixture()
def pg_conn():
    conn = psycopg.connect(_DB_URL, autocommit=False)
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


@pytest.fixture()
def tenant_id(pg_conn):
    email = "s6b-reconcile-tests@example.com"
    with pg_conn.cursor() as cur:
        cur.execute("SELECT id FROM users WHERE lower(email) = lower(%s)", (email,))
        row = cur.fetchone()
        user_id = row[0] if row else None
        if user_id is None:
            cur.execute("INSERT INTO users (email) VALUES (%s) RETURNING id", (email,))
            user_id = cur.fetchone()[0]
        cur.execute("SELECT id FROM tenants WHERE personal_user_id = %s", (user_id,))
        row = cur.fetchone()
        result = row[0] if row else None
        if result is None:
            cur.execute("INSERT INTO tenants (kind, personal_user_id) VALUES ('personal', %s) RETURNING id", (user_id,))
            result = cur.fetchone()[0]
    pg_conn.commit()
    return result


def _payload_for(tenant_id: int, org_login: str = "acme") -> str:
    enc = encrypt_job_token("secret", settings.job_secret_key.get_secret_value())
    return json.dumps({"tenant_id": tenant_id, "org_login": org_login, "token": enc})


def _org_members(conn, tenant_id):
    with conn.cursor() as cur:
        cur.execute(f"SET app.tenant_id = {int(tenant_id)}")
        cur.execute(
            "SELECT login, role, two_factor_enabled FROM org_members WHERE tenant_id = %s ORDER BY login", (tenant_id,)
        )
        return cur.fetchall()


def _sync_cursor(conn, tenant_id):
    with conn.cursor() as cur:
        cur.execute(f"SET app.tenant_id = {int(tenant_id)}")
        cur.execute(
            "SELECT org_login, last_synced_at FROM org_membership_sync_cursors WHERE tenant_id = %s", (tenant_id,)
        )
        return cur.fetchone()


def test_handler_inserts_members_and_upserts_cursor(pg_conn, tenant_id):
    roster = {
        "members": [
            {"login": "reconcile-owner", "avatar_url": "a", "role": "admin"},
            {"login": "reconcile-member", "avatar_url": "b", "role": "member"},
        ],
        # Checked, only reconcile-member has 2FA disabled -- reconcile-owner is therefore
        # enabled=True, reconcile-member enabled=False (both known, not None -- the overlay
        # call itself succeeded).
        "two_factor_disabled_logins": {"reconcile-member"},
        "outside_logins": set(),
    }

    with patch("worker.membership_reconcile.fetch_org_roster", return_value=roster), patch("worker.httpx.Client"):
        worker._handle_reconcile_org_membership(pg_conn, 100, _payload_for(tenant_id), 0)

    rows = _org_members(pg_conn, tenant_id)
    assert rows == [
        ("reconcile-member", "member", False),
        ("reconcile-owner", "admin", True),
    ]
    cursor = _sync_cursor(pg_conn, tenant_id)
    assert cursor[0] == "acme"
    assert cursor[1] is not None


def test_handler_removes_a_member_no_longer_in_the_roster(pg_conn, tenant_id):
    first = {
        "members": [
            {"login": "reconcile-leaver", "avatar_url": "a", "role": "member"},
            {"login": "reconcile-stayer", "avatar_url": "b", "role": "member"},
        ],
        "two_factor_disabled_logins": {"reconcile-leaver", "reconcile-stayer"},
        "outside_logins": set(),
    }
    # Second poll's roster no longer includes reconcile-leaver -- a real departure the
    # removal webhook missed. Still non-empty (reconcile-stayer remains), so this exercises
    # actual reconciliation-driven removal, not the empty-roster no-op guard below.
    second = {
        "members": [{"login": "reconcile-stayer", "avatar_url": "b", "role": "member"}],
        "two_factor_disabled_logins": {"reconcile-stayer"},
        "outside_logins": set(),
    }

    with patch("worker.membership_reconcile.fetch_org_roster", return_value=first), patch("worker.httpx.Client"):
        worker._handle_reconcile_org_membership(pg_conn, 101, _payload_for(tenant_id, "leaver-org"), 0)
    assert {r[0] for r in _org_members(pg_conn, tenant_id)} == {"reconcile-leaver", "reconcile-stayer"}

    with patch("worker.membership_reconcile.fetch_org_roster", return_value=second), patch("worker.httpx.Client"):
        worker._handle_reconcile_org_membership(pg_conn, 102, _payload_for(tenant_id, "leaver-org"), 0)
    assert {r[0] for r in _org_members(pg_conn, tenant_id)} == {"reconcile-stayer"}


def test_handler_empty_roster_does_not_wipe_existing_members(pg_conn, tenant_id):
    with pg_conn.cursor() as cur:
        cur.execute(f"SET app.tenant_id = {int(tenant_id)}")
        cur.execute(
            "INSERT INTO repo_collaborators (tenant_id, repo, login, permission, source, is_outside_collaborator, granted_at) "
            "VALUES (%s, 'acme/empty-org-repo', 'reconcile-survivor', 'push', 'direct', FALSE, %s) "
            "ON CONFLICT (tenant_id, repo, login) DO UPDATE SET is_outside_collaborator = FALSE",
            (tenant_id, datetime.now(timezone.utc)),
        )

    seeded = {
        "members": [{"login": "reconcile-survivor", "avatar_url": "a", "role": "member"}],
        "two_factor_disabled_logins": set(),
        "outside_logins": set(),
    }
    empty = {"members": [], "two_factor_disabled_logins": set(), "outside_logins": set()}

    with patch("worker.membership_reconcile.fetch_org_roster", return_value=seeded), patch("worker.httpx.Client"):
        worker._handle_reconcile_org_membership(pg_conn, 110, _payload_for(tenant_id, "empty-org"), 0)
    assert len(_org_members(pg_conn, tenant_id)) == 1

    # A genuinely empty roster from the GitHub API is treated as a no-op, not "remove
    # everyone" -- see org_membership_store.reconcile_org_members's docstring for why (an
    # org always has at least one owner, so an empty response is almost certainly a
    # transient upstream problem, not a real zero-member org). Same posture for
    # reconcile_repo_collaborator_outside_status: an empty member_logins set must not mark
    # every existing repo_collaborators row as outside.
    with patch("worker.membership_reconcile.fetch_org_roster", return_value=empty), patch("worker.httpx.Client"):
        worker._handle_reconcile_org_membership(pg_conn, 111, _payload_for(tenant_id, "empty-org"), 0)
    assert len(_org_members(pg_conn, tenant_id)) == 1

    with pg_conn.cursor() as cur:
        cur.execute(f"SET app.tenant_id = {int(tenant_id)}")
        cur.execute(
            "SELECT is_outside_collaborator FROM repo_collaborators WHERE tenant_id = %s AND login = 'reconcile-survivor'",
            (tenant_id,),
        )
        (is_outside,) = cur.fetchone()
    assert is_outside is False


def test_handler_backfills_is_outside_collaborator_on_existing_repo_collaborators(pg_conn, tenant_id):
    # ON CONFLICT DO UPDATE, not a plain INSERT: the handler below commits internally on
    # this same connection (like every other real-Postgres test in this file), so a plain
    # INSERT here would only be idempotent on the very first run against a given DB volume --
    # a second run in the same session would hit uq_repo_collaborators_tenant_repo_login.
    # This reseeds is_outside_collaborator back to NULL regardless of prior runs.
    with pg_conn.cursor() as cur:
        cur.execute(f"SET app.tenant_id = {int(tenant_id)}")
        cur.execute(
            "INSERT INTO repo_collaborators (tenant_id, repo, login, permission, source, is_outside_collaborator, granted_at) "
            "VALUES (%s, 'acme/widgets', 'direct-member', 'push', 'direct', NULL, %s), "
            "(%s, 'acme/widgets', 'contractor', 'push', 'direct', NULL, %s) "
            "ON CONFLICT (tenant_id, repo, login) DO UPDATE SET is_outside_collaborator = NULL",
            (tenant_id, datetime.now(timezone.utc), tenant_id, datetime.now(timezone.utc)),
        )

    roster = {
        "members": [{"login": "direct-member", "avatar_url": "a", "role": "member"}],
        "two_factor_disabled_logins": set(),
        "outside_logins": {"contractor"},
    }

    with patch("worker.membership_reconcile.fetch_org_roster", return_value=roster), patch("worker.httpx.Client"):
        worker._handle_reconcile_org_membership(pg_conn, 103, _payload_for(tenant_id, "outside-org"), 0)

    with pg_conn.cursor() as cur:
        cur.execute(f"SET app.tenant_id = {int(tenant_id)}")
        # Scoped to this test's own repo, not "every row for this tenant" -- tenant_id is a
        # shared get-or-create fixture across this whole test file (real commits, not rolled
        # back), so other tests' seeded repo_collaborators rows for other repos coexist here.
        cur.execute(
            "SELECT login, is_outside_collaborator FROM repo_collaborators "
            "WHERE tenant_id = %s AND repo = 'acme/widgets' ORDER BY login",
            (tenant_id,),
        )
        rows = cur.fetchall()
    assert rows == [("contractor", True), ("direct-member", False)]


def test_handler_preserves_two_factor_enabled_when_the_overlay_is_unavailable(pg_conn, tenant_id):
    first = {
        "members": [{"login": "reconcile-2fa", "avatar_url": "a", "role": "member"}],
        "two_factor_disabled_logins": set(),  # overlay succeeded, nobody disabled -> enabled=True
        "outside_logins": set(),
    }
    with patch("worker.membership_reconcile.fetch_org_roster", return_value=first), patch("worker.httpx.Client"):
        worker._handle_reconcile_org_membership(pg_conn, 104, _payload_for(tenant_id, "twofa-org"), 0)
    assert _org_members(pg_conn, tenant_id)[0][2] is True

    # Second poll's 2FA overlay call failed (fetch_org_roster returns None for it) --
    # two_factor_enabled must stay True, not be clobbered to NULL.
    second = {
        "members": [{"login": "reconcile-2fa", "avatar_url": "a", "role": "member"}],
        "two_factor_disabled_logins": None,
        "outside_logins": set(),
    }
    with patch("worker.membership_reconcile.fetch_org_roster", return_value=second), patch("worker.httpx.Client"):
        worker._handle_reconcile_org_membership(pg_conn, 105, _payload_for(tenant_id, "twofa-org"), 0)
    assert _org_members(pg_conn, tenant_id)[0][2] is True
