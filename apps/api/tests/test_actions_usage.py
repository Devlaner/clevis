"""Tests for GET /orgs/{org}/usage/actions (issue #294).

Admin-only. Reads GitHub's enhanced-billing usage summary
(``GET /organizations/{org}/settings/billing/usage/summary?product=actions`` — the
older ``/settings/billing/actions`` endpoint was retired 2025-09-26), which needs an
App permission Clevis doesn't request by default: a 403 from GitHub is turned into a
400 with a clear hint so the UI can hide the card instead of erroring the page.
"""

from datetime import datetime, timezone
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.core.auth import UserOut, require_auth
from src.core.db import User, get_db
from src.repositories import org_membership_repo, org_repo
from src.routers.analytics import router


def _make_user(db, email: str) -> UserOut:
    user = User(email=email, name=None, password_hash=None, is_workspace_admin=False)
    db.add(user)
    db.commit()
    db.refresh(user)
    return UserOut(id=user.id, email=user.email, name=user.name, is_workspace_admin=False)


@pytest.fixture()
def user(db) -> UserOut:
    return _make_user(db, "usage@example.com")


@pytest.fixture()
def http(db, user) -> TestClient:
    app = FastAPI()
    app.dependency_overrides[require_auth] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    app.include_router(router)
    return TestClient(app)


def _admin_org(db, user):
    org = org_repo.get_or_create(db, github_login="acme")
    org_membership_repo.get_or_create(db, org_id=org.id, user_id=user.id, role="admin")
    db.commit()
    return org


# GitHub's usage-summary shape for an Actions-filtered query: pre-aggregated per SKU,
# plus an Actions *storage* line (unitType "GB") that must be ignored.
_USAGE_SUMMARY = {
    "timePeriod": {"year": 2026, "month": 9},
    "organization": "acme",
    "usageItems": [
        {
            "product": "Actions",
            "sku": "actions_linux",
            "unitType": "minutes",
            "pricePerUnit": 0.008,
            "grossQuantity": 1000,
            "discountQuantity": 900,
            "netQuantity": 100,
        },
        {
            "product": "Actions",
            "sku": "actions_macos",
            "unitType": "minutes",
            "pricePerUnit": 0.08,
            "grossQuantity": 250,
            "discountQuantity": 0,
            "netQuantity": 250,
        },
        {
            "product": "Actions",
            "sku": "actions_storage",
            "unitType": "GB",
            "pricePerUnit": 0.25,
            "grossQuantity": 5,
            "discountQuantity": 5,
            "netQuantity": 0,
        },
    ],
}


def test_outsider_forbidden(http, db):
    org_repo.get_or_create(db, github_login="acme")
    assert http.get("/orgs/acme/usage/actions").status_code == 403


def test_member_forbidden(http, db, user):
    org = org_repo.get_or_create(db, github_login="acme")
    org_membership_repo.get_or_create(db, org_id=org.id, user_id=user.id, role="member")
    db.commit()
    assert http.get("/orgs/acme/usage/actions").status_code == 403


def test_admin_gets_shaped_usage_from_the_summary_endpoint(http, db, user):
    _admin_org(db, user)
    with patch("src.routers.analytics.GitHubClient") as mock_client:
        mock_client.return_value.request.return_value = _USAGE_SUMMARY
        resp = http.get("/orgs/acme/usage/actions", headers={"X-GitHub-Token": "ghp_admin"})

    assert resp.status_code == 200
    body = resp.json()
    # minutes lines only: 1000 + 250 gross; storage (GB) ignored
    assert body["total_minutes_used"] == 1250
    assert body["included_minutes_used"] == 900  # 900 + 0
    assert body["paid_minutes_used"] == 350  # 100 + 250
    assert body["minutes_used_breakdown"] == {"actions_linux": 1000, "actions_macos": 250}
    # billing data must never be cached
    assert resp.headers["cache-control"] == "no-store"


def test_unit_type_match_is_case_insensitive(http, db, user):
    _admin_org(db, user)
    payload = {
        "usageItems": [
            {"sku": "actions_linux", "unitType": "Minutes", "grossQuantity": 500,
             "discountQuantity": 500, "netQuantity": 0},
        ]
    }
    with patch("src.routers.analytics.GitHubClient") as mock_client:
        mock_client.return_value.request.return_value = payload
        resp = http.get("/orgs/acme/usage/actions", headers={"X-GitHub-Token": "ghp_x"})
    assert resp.status_code == 200
    assert resp.json()["total_minutes_used"] == 500


def test_calls_the_enhanced_billing_usage_summary_endpoint_for_the_current_month(http, db, user):
    _admin_org(db, user)
    # Freeze the clock: the route reads datetime.now() again during handling, so a real
    # clock could straddle a UTC month boundary between here and the assertion below.
    frozen = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
    with patch("src.routers.analytics.GitHubClient") as mock_client, patch(
        "src.routers.analytics.datetime"
    ) as mock_dt:
        mock_dt.now.return_value = frozen
        mock_client.return_value.request.return_value = _USAGE_SUMMARY
        http.get("/orgs/acme/usage/actions", headers={"X-GitHub-Token": "ghp_admin"})

    mock_client.return_value.request.assert_called_once_with(
        "GET",
        "/organizations/acme/settings/billing/usage/summary",
        params={"year": 2026, "month": 9, "product": "actions"},
    )


def test_present_but_non_numeric_billing_quantity_is_502(http, db, user):
    _admin_org(db, user)
    payload = {
        "usageItems": [
            {"sku": "actions_linux", "unitType": "minutes", "grossQuantity": "lots",
             "discountQuantity": 0, "netQuantity": 0},
        ]
    }
    with patch("src.routers.analytics.GitHubClient") as mock_client:
        mock_client.return_value.request.return_value = payload
        resp = http.get("/orgs/acme/usage/actions", headers={"X-GitHub-Token": "ghp_x"})
    assert resp.status_code == 502


def test_a_missing_billing_quantity_is_treated_as_zero(http, db, user):
    _admin_org(db, user)
    payload = {"usageItems": [{"sku": "actions_linux", "unitType": "minutes", "grossQuantity": 42}]}
    with patch("src.routers.analytics.GitHubClient") as mock_client:
        mock_client.return_value.request.return_value = payload
        resp = http.get("/orgs/acme/usage/actions", headers={"X-GitHub-Token": "ghp_x"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_minutes_used"] == 42
    assert body["included_minutes_used"] == 0
    assert body["paid_minutes_used"] == 0


def test_github_403_becomes_400_with_permission_hint(http, db, user):
    _admin_org(db, user)
    err = httpx.HTTPStatusError(
        "403", request=httpx.Request("GET", "https://api.github.com"), response=httpx.Response(403)
    )
    with patch("src.routers.analytics.GitHubClient") as mock_client:
        mock_client.return_value.request.side_effect = err
        resp = http.get("/orgs/acme/usage/actions", headers={"X-GitHub-Token": "ghp_noscope"})

    assert resp.status_code == 400
    assert "Administration" in resp.json()["detail"]


def test_no_token_available_returns_400(http, db, user):
    _admin_org(db, user)  # admin, but no installation and no client token
    resp = http.get("/orgs/acme/usage/actions")
    assert resp.status_code == 400


def test_non_403_github_status_error_maps_without_the_permission_hint(http, db, user):
    _admin_org(db, user)
    err = httpx.HTTPStatusError(
        "500", request=httpx.Request("GET", "https://api.github.com"), response=httpx.Response(500)
    )
    with patch("src.routers.analytics.GitHubClient") as mock_client:
        mock_client.return_value.request.side_effect = err
        resp = http.get("/orgs/acme/usage/actions", headers={"X-GitHub-Token": "ghp_x"})
    assert resp.status_code >= 400
    assert "Administration" not in resp.json()["detail"]


def test_github_network_error_is_surfaced(http, db, user):
    _admin_org(db, user)
    with patch("src.routers.analytics.GitHubClient") as mock_client:
        mock_client.return_value.request.side_effect = httpx.ConnectError("boom")
        resp = http.get("/orgs/acme/usage/actions", headers={"X-GitHub-Token": "ghp_x"})
    assert resp.status_code >= 500


def test_top_level_shape_without_usage_items_is_502(http, db, user):
    _admin_org(db, user)
    with patch("src.routers.analytics.GitHubClient") as mock_client:
        mock_client.return_value.request.return_value = {"organization": "acme"}
        resp = http.get("/orgs/acme/usage/actions", headers={"X-GitHub-Token": "ghp_x"})
    assert resp.status_code == 502


def test_non_dict_usage_item_is_502(http, db, user):
    _admin_org(db, user)
    with patch("src.routers.analytics.GitHubClient") as mock_client:
        mock_client.return_value.request.return_value = {"usageItems": ["nope"]}
        resp = http.get("/orgs/acme/usage/actions", headers={"X-GitHub-Token": "ghp_x"})
    assert resp.status_code == 502


def test_list_top_level_shape_is_502(http, db, user):
    _admin_org(db, user)
    with patch("src.routers.analytics.GitHubClient") as mock_client:
        mock_client.return_value.request.return_value = ["not", "a", "dict"]
        resp = http.get("/orgs/acme/usage/actions", headers={"X-GitHub-Token": "ghp_x"})
    assert resp.status_code == 502


def test_storage_only_usage_yields_zeroes(http, db, user):
    _admin_org(db, user)
    storage_only = {
        "usageItems": [
            {"sku": "actions_storage", "unitType": "GB", "grossQuantity": 12,
             "discountQuantity": 0, "netQuantity": 12},
        ]
    }
    with patch("src.routers.analytics.GitHubClient") as mock_client:
        mock_client.return_value.request.return_value = storage_only
        resp = http.get("/orgs/acme/usage/actions", headers={"X-GitHub-Token": "ghp_x"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_minutes_used"] == 0
    assert body["minutes_used_breakdown"] == {}
