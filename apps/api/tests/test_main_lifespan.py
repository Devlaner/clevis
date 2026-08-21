"""Tests for the API lifespan's gap-heal background task wiring (issue #192/S5 PR 2)."""

import asyncio
from unittest.mock import patch

import pytest
from fastapi import FastAPI

from src.main import lifespan


@pytest.mark.asyncio
async def test_lifespan_starts_and_cleanly_cancels_the_gap_heal_loop():
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def fake_loop():
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    with patch("src.main.gap_heal_loop", side_effect=fake_loop):
        async with lifespan(FastAPI()):
            await asyncio.wait_for(started.wait(), timeout=1)

    assert cancelled.is_set()
