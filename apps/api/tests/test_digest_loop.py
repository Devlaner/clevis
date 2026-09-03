"""Tests for the leadership-digest background loop (issue #292)."""

import asyncio
from unittest.mock import patch

import pytest

from src.services import digest_loop


def test_read_poll_seconds_default():
    with patch("src.services.digest_loop.get_config", return_value="3600"):
        assert digest_loop._read_poll_seconds() == 3600


def test_read_poll_seconds_clamps():
    with patch("src.services.digest_loop.get_config", return_value="1"):
        assert digest_loop._read_poll_seconds() == 300
    with patch("src.services.digest_loop.get_config", return_value="99999999"):
        assert digest_loop._read_poll_seconds() == 86_400


def test_read_poll_seconds_falls_back_on_non_integer():
    with patch("src.services.digest_loop.get_config", return_value="weekly"):
        assert digest_loop._read_poll_seconds() == 3600


class _FakeSession:
    def __init__(self):
        self.executed = []
        self.committed = False

    def execute(self, stmt, params=None):
        self.executed.append(str(stmt))

    def rollback(self):
        pass

    def commit(self):
        self.committed = True

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def test_run_sweep_resets_tenant_context_even_when_the_sweep_raises():
    fake_db = _FakeSession()
    with (
        patch("src.services.digest_loop.SessionLocal", return_value=fake_db),
        patch("src.services.digest_loop.run_digest_sweep", side_effect=RuntimeError("boom")),
        pytest.raises(RuntimeError),
    ):
        digest_loop._run_sweep()
    assert any("RESET app.tenant_id" in s for s in fake_db.executed)
    assert fake_db.committed is True


@pytest.mark.asyncio
async def test_loop_survives_a_sweep_exception_and_still_sleeps():
    calls = {"sweep": 0, "sleep": 0}

    async def fake_to_thread(_fn):
        calls["sweep"] += 1
        raise RuntimeError("simulated")

    async def fake_sleep(_seconds):
        calls["sleep"] += 1
        raise asyncio.CancelledError()

    with (
        patch("src.services.digest_loop.asyncio.to_thread", side_effect=fake_to_thread),
        patch("src.services.digest_loop.asyncio.sleep", side_effect=fake_sleep),
        patch("src.services.digest_loop.get_config", return_value="3600"),
        pytest.raises(asyncio.CancelledError),
    ):
        await digest_loop.digest_loop()

    assert calls == {"sweep": 1, "sleep": 1}
