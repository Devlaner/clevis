"""Tests for the gap-heal background loop (issue #192/S5 PR 2)."""

import asyncio
from unittest.mock import patch

import pytest

from src.services import gap_heal_loop


def test_read_poll_seconds_default():
    with patch("src.services.gap_heal_loop.get_config", return_value="900"):
        assert gap_heal_loop._read_poll_seconds() == 900


def test_read_poll_seconds_clamps_high_values():
    with patch("src.services.gap_heal_loop.get_config", return_value="999999"):
        assert gap_heal_loop._read_poll_seconds() == 3600


def test_read_poll_seconds_clamps_low_values():
    with patch("src.services.gap_heal_loop.get_config", return_value="1"):
        assert gap_heal_loop._read_poll_seconds() == 60


def test_read_poll_seconds_falls_back_on_non_integer():
    with patch("src.services.gap_heal_loop.get_config", return_value="nope"):
        assert gap_heal_loop._read_poll_seconds() == 900


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
    # _run_sweep uses SessionLocal() directly, bypassing get_db()'s own finally-block
    # reset -- without this, a connection returned to the pool after processing tenant N
    # would leak tenant N's app.tenant_id into whichever request or sweep iteration checks
    # that connection out next (issue found on this PR's own review).
    fake_db = _FakeSession()

    with (
        patch("src.services.gap_heal_loop.SessionLocal", return_value=fake_db),
        patch("src.services.gap_heal_loop.run_gap_heal_sweep", side_effect=RuntimeError("boom")),
        pytest.raises(RuntimeError),
    ):
        gap_heal_loop._run_sweep()

    assert any("RESET app.tenant_id" in s for s in fake_db.executed)
    assert any("RESET app.user_id" in s for s in fake_db.executed)
    assert fake_db.committed is True


@pytest.mark.asyncio
async def test_loop_runs_the_sweep_then_sleeps_each_iteration():
    # asyncio.sleep raising is what ends the (otherwise infinite) loop for this test --
    # the real code has no other exit point, matching worker.py's own while-True shape.
    calls = {"sweep": 0, "sleep": 0}

    async def fake_to_thread(_fn):
        calls["sweep"] += 1

    async def fake_sleep(_seconds):
        calls["sleep"] += 1
        raise asyncio.CancelledError()

    with (
        patch("src.services.gap_heal_loop.asyncio.to_thread", side_effect=fake_to_thread),
        patch("src.services.gap_heal_loop.asyncio.sleep", side_effect=fake_sleep),
        patch("src.services.gap_heal_loop.get_config", return_value="900"),
        pytest.raises(asyncio.CancelledError),
    ):
        await gap_heal_loop.gap_heal_loop()

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
        patch("src.services.gap_heal_loop.asyncio.to_thread", side_effect=fake_to_thread),
        patch("src.services.gap_heal_loop.asyncio.sleep", side_effect=fake_sleep),
        patch("src.services.gap_heal_loop.get_config", return_value="900"),
        pytest.raises(asyncio.CancelledError),
    ):
        await gap_heal_loop.gap_heal_loop()

    # The loop must reach its sleep even after the sweep itself raised -- otherwise a
    # single bad iteration would kill the loop forever instead of just logging and
    # retrying next cycle.
    assert calls == {"sweep": 1, "sleep": 1}
