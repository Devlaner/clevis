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
