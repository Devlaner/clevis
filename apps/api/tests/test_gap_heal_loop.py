"""Tests for the gap-heal background loop's poll-interval clamping (issue #192/S5 PR 2)."""

from unittest.mock import patch

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
