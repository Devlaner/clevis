"""Tests for the org-membership reconciliation background loop (Collaborators PR 2 of 3)."""

import asyncio
from unittest.mock import patch

import pytest

from src.services import membership_reconcile_loop


def test_read_poll_seconds_default():
    with patch("src.services.membership_reconcile_loop.get_config", return_value="900"):
        assert membership_reconcile_loop._read_poll_seconds() == 900


def test_read_poll_seconds_clamps_high_values():
    with patch("src.services.membership_reconcile_loop.get_config", return_value="999999"):
        assert membership_reconcile_loop._read_poll_seconds() == 3600


def test_read_poll_seconds_clamps_low_values():
    with patch("src.services.membership_reconcile_loop.get_config", return_value="1"):
        assert membership_reconcile_loop._read_poll_seconds() == 60


def test_read_poll_seconds_falls_back_on_non_integer():
    with patch("src.services.membership_reconcile_loop.get_config", return_value="nope"):
        assert membership_reconcile_loop._read_poll_seconds() == 900


class _FakeSession:
    def __init__(self):
        self.executed = []
        self.rolled_back = False
        self.committed = False

    def execute(self, stmt, params=None):
        self.executed.append(str(stmt))

    def rollback(self):
        self.rolled_back = True

    def commit(self):
        self.committed = True

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def test_run_sweep_resets_the_tenant_session_context_even_when_the_sweep_raises():
    # Same leak this repo already fixed once for gap_heal_loop.py's own _run_sweep -- see
    # that module's test with the identical name for the full reasoning.
    fake_db = _FakeSession()

    with (
        patch("src.services.membership_reconcile_loop.SessionLocal", return_value=fake_db),
        patch("src.services.membership_reconcile_loop.run_membership_reconcile_sweep", side_effect=RuntimeError("boom")),
        pytest.raises(RuntimeError),
    ):
        membership_reconcile_loop._run_sweep()

    assert any("RESET app.tenant_id" in s for s in fake_db.executed)
    assert any("RESET app.user_id" in s for s in fake_db.executed)
    assert fake_db.committed is True


@pytest.mark.asyncio
async def test_loop_runs_the_sweep_then_sleeps_each_iteration():
    calls = {"sweep": 0, "sleep": 0}

    async def fake_to_thread(_fn):
        calls["sweep"] += 1

    async def fake_sleep(_seconds):
        calls["sleep"] += 1
        raise asyncio.CancelledError()

    with (
        patch("src.services.membership_reconcile_loop.asyncio.to_thread", side_effect=fake_to_thread),
        patch("src.services.membership_reconcile_loop.asyncio.sleep", side_effect=fake_sleep),
        patch("src.services.membership_reconcile_loop.get_config", return_value="900"),
        pytest.raises(asyncio.CancelledError),
    ):
        await membership_reconcile_loop.membership_reconcile_loop()

    assert calls == {"sweep": 1, "sleep": 1}


@pytest.mark.asyncio
async def test_loop_survives_an_exception_from_the_sweep_and_still_sleeps():
    calls = {"sweep": 0, "sleep": 0}

    async def fake_to_thread(_fn):
        calls["sweep"] += 1
        raise RuntimeError("simulated sweep failure")

    async def fake_sleep(_seconds):
        calls["sleep"] += 1
        raise asyncio.CancelledError()

    with (
        patch("src.services.membership_reconcile_loop.asyncio.to_thread", side_effect=fake_to_thread),
        patch("src.services.membership_reconcile_loop.asyncio.sleep", side_effect=fake_sleep),
        patch("src.services.membership_reconcile_loop.get_config", return_value="900"),
        pytest.raises(asyncio.CancelledError),
    ):
        await membership_reconcile_loop.membership_reconcile_loop()

    assert calls == {"sweep": 1, "sleep": 1}
