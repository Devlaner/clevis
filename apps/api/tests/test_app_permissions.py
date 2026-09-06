"""Unit tests for the GitHub App permission manifest / drift computation."""

from src.services import app_permissions


def test_satisfies_exact_and_higher_level():
    assert app_permissions.satisfies({"issues": "write"}, {"issues": "write"})
    # admin covers a write requirement
    assert app_permissions.satisfies({"administration": "admin"}, {"administration": "write"})
    # write covers a read requirement
    assert app_permissions.satisfies({"contents": "write"}, {"contents": "read"})


def test_missing_permissions_reports_gap():
    assert app_permissions.missing_permissions({"issues": "read"}, {"issues": "write"}) == {"issues": "write"}
    assert app_permissions.missing_permissions(None, {"pull_requests": "write"}) == {"pull_requests": "write"}
    assert app_permissions.missing_permissions({}, {"contents": "write", "workflows": "write"}) == {
        "contents": "write",
        "workflows": "write",
    }
    assert app_permissions.missing_permissions({"contents": "write"}, {"contents": "write"}) == {}


def test_blocked_features_none_granted_blocks_everything():
    blocked = app_permissions.blocked_features(None)
    assert {b.feature for b in blocked} == set(app_permissions.FEATURE_PERMISSIONS)


def test_blocked_features_partial_grant():
    # Only Pull requests: write granted — unblocks stale_pr_nudges, still blocks the rest.
    blocked = {b.feature: b.missing for b in app_permissions.blocked_features({"pull_requests": "write"})}
    assert "stale_pr_nudges" not in blocked
    assert blocked["dependabot_triage"] == {"contents": "write"}
    assert blocked["bulk_branch_protection"] == {"administration": "write"}


def test_blocked_features_full_write_grant_unblocks_all():
    granted = {
        "issues": "write",
        "administration": "write",
        "vulnerability_alerts": "write",
        "pull_requests": "write",
        "contents": "write",
        "workflows": "write",
        "actions": "write",
    }
    assert app_permissions.blocked_features(granted) == []


def test_workflow_dispatch_needs_actions_write():
    # Actions: read (enough to list workflows) does not satisfy the dispatch write need.
    blocked = {b.feature: b.missing for b in app_permissions.blocked_features({"actions": "read"})}
    assert blocked["workflow_dispatch"] == {"actions": "write"}
    assert "workflow_dispatch" not in {
        b.feature for b in app_permissions.blocked_features({"actions": "write"})
    }


def test_blocked_features_order_is_stable():
    blocked = app_permissions.blocked_features({})
    assert [b.feature for b in blocked] == list(app_permissions.FEATURE_PERMISSIONS)
