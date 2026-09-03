"""
Runtime configuration backed by the app_config DB table.

Values are cached in memory for 60 seconds. The cache is deliberately simple
(a module-level dict) to avoid circular imports. Thread safety is not critical
here — a brief race on TTL expiry means at most one extra DB read.

Usage:
    from src.core.app_config import get_config

    poll = get_config("worker_poll_seconds", "5")
"""

import logging
import time

from sqlalchemy import text

logger = logging.getLogger(__name__)

_ACCEPTED_KEYS = {
    "worker_poll_seconds",
    "registration_enabled",
    "gap_heal_poll_seconds",
    "gap_heal_stale_hours",
    "membership_reconcile_poll_seconds",
    "membership_reconcile_stale_hours",
    # Issue #289: stale-PR nudges (on-demand sweep)
    "pr_nudge_stale_days",
    "pr_nudge_mode",
}
_TTL = 60.0
_cache: dict[str, tuple[str, float]] = {}


def get_config(key: str, default: str = "") -> str:
    """Return the value for *key* from app_config, falling back to *default*."""
    now = time.monotonic()
    if key in _cache:
        val, ts = _cache[key]
        if now - ts < _TTL:
            return val

    # Import here to avoid circular dependency (db imports config)
    from src.core.db import SessionLocal  # noqa: PLC0415

    try:
        with SessionLocal() as db:
            row = db.execute(
                text("SELECT value FROM app_config WHERE key = :key"), {"key": key}
            ).fetchone()
        val = row[0] if row else default
    except Exception:
        logger.warning("app_config read failed for key %r, using default", key)
        val = default

    _cache[key] = (val, now)
    return val


def read_all() -> dict[str, str]:
    """Return all app_config rows as a plain dict (no caching)."""
    from src.core.db import SessionLocal  # noqa: PLC0415

    with SessionLocal() as db:
        rows = db.execute(text("SELECT key, value FROM app_config")).fetchall()
    return {r[0]: r[1] for r in rows}


def set_config(key: str, value: str) -> None:
    """Persist *key* → *value* and invalidate its cache entry.

    Uses an upsert so a missing row (e.g. manually deleted) is re-created rather
    than silently ignored by a plain UPDATE.
    """
    if key not in _ACCEPTED_KEYS:
        raise ValueError(f"Unknown config key: {key!r}")

    from src.core.db import SessionLocal  # noqa: PLC0415

    with SessionLocal() as db:
        db.execute(
            text(
                "INSERT INTO app_config (key, value, updated_at) VALUES (:key, :value, NOW()) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()"
            ),
            {"key": key, "value": value},
        )
        db.commit()

    _cache.pop(key, None)
