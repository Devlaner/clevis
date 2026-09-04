"""Issue #292: asyncio background loop that periodically runs the digest sweep.

Mirrors gap_heal_loop.py exactly -- same clamp/poll/reset shape. Started from
src.main's lifespan. The sweep itself is a no-op unless the `digest_cadence`
instance-config key is set to weekly or monthly.
"""

import asyncio
import logging

from sqlalchemy import text

from src.core.app_config import get_config
from src.core.db import SessionLocal
from src.services.digest_sweep import run_digest_sweep

log = logging.getLogger(__name__)

_MAX_POLL_SECONDS = 86_400
_DEFAULT_POLL_SECONDS = 3_600


def _read_poll_seconds() -> int:
    raw = get_config("digest_poll_seconds", str(_DEFAULT_POLL_SECONDS))
    try:
        return max(300, min(_MAX_POLL_SECONDS, int(raw)))
    except ValueError:
        log.warning("digest_poll_seconds %r is not an integer; using %d", raw, _DEFAULT_POLL_SECONDS)
        return _DEFAULT_POLL_SECONDS


def _run_sweep() -> None:
    with SessionLocal() as db:
        try:
            run_digest_sweep(db)
        finally:
            # Same reasoning as gap_heal_loop._run_sweep: the sweep sets app.tenant_id via
            # plain SET and commits, so reset it before this pooled connection is reused.
            db.rollback()
            db.execute(text("RESET app.tenant_id"))
            db.execute(text("RESET app.user_id"))
            db.commit()


async def digest_loop() -> None:
    log.info("leadership-digest sweep loop started")
    while True:
        try:
            await asyncio.to_thread(_run_sweep)
        except Exception:
            log.exception("digest sweep iteration failed")
        await asyncio.sleep(_read_poll_seconds())
