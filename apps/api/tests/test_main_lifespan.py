"""Tests for the API lifespan's background task wiring: gap-heal (issue #192/S5 PR 2) and
org-membership reconciliation (Collaborators PR 2 of 3), two independent loops."""

import asyncio
from unittest.mock import patch

import pytest
from fastapi import FastAPI

from src.main import lifespan


def _fake_loop(started: asyncio.Event, cancelled: asyncio.Event):
    async def run():
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    return run


@pytest.mark.asyncio
async def test_lifespan_starts_and_cleanly_cancels_the_gap_heal_loop():
    started = asyncio.Event()
    cancelled = asyncio.Event()
    other_started = asyncio.Event()
    other_cancelled = asyncio.Event()

    with (
        patch("src.main.gap_heal_loop", side_effect=_fake_loop(started, cancelled)),
        patch("src.main.membership_reconcile_loop", side_effect=_fake_loop(other_started, other_cancelled)),
    ):
        async with lifespan(FastAPI()):
            await asyncio.wait_for(started.wait(), timeout=1)

    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_lifespan_starts_and_cleanly_cancels_the_membership_reconcile_loop():
    started = asyncio.Event()
    cancelled = asyncio.Event()
    other_started = asyncio.Event()
    other_cancelled = asyncio.Event()

    with (
        patch("src.main.gap_heal_loop", side_effect=_fake_loop(other_started, other_cancelled)),
        patch("src.main.membership_reconcile_loop", side_effect=_fake_loop(started, cancelled)),
    ):
        async with lifespan(FastAPI()):
            await asyncio.wait_for(started.wait(), timeout=1)

    assert cancelled.is_set()
