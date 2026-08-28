"""Collaborators PR 2 of 3: asyncio background loop that periodically runs the membership
reconciliation sweep. Mirrors gap_heal_loop.py exactly -- see that module's docstring for the
pattern (the API is already a long-running uvicorn process, so this is the smallest thing
that satisfies "runs periodically" without a new scheduler dependency or container). Started
from src.main's lifespan alongside gap_heal_loop, as a second independent task -- the two
sweeps are unrelated (activity sync vs. org membership) and each already tolerates a single
iteration's exception without dying, so there's no reason to share one loop.
"""

import asyncio
import logging

from sqlalchemy import text

from src.core.app_config import get_config
from src.core.db import SessionLocal
from src.services.membership_reconcile_sweep import run_membership_reconcile_sweep

log = logging.getLogger(__name__)

# Same reasoning as gap_heal_loop.py's own clamp -- an upper bound so a misconfigured
# app_config value can't make the loop effectively never run.
_MAX_POLL_SECONDS = 3600
_DEFAULT_POLL_SECONDS = 900


def _read_poll_seconds() -> int:
    raw = get_config("membership_reconcile_poll_seconds", str(_DEFAULT_POLL_SECONDS))
    try:
        return max(60, min(_MAX_POLL_SECONDS, int(raw)))
    except ValueError:
        log.warning("membership_reconcile_poll_seconds %r is not an integer; using %d", raw, _DEFAULT_POLL_SECONDS)
        return _DEFAULT_POLL_SECONDS


def _run_sweep() -> None:
    with SessionLocal() as db:
        try:
            run_membership_reconcile_sweep(db)
        finally:
            # Mirrors gap_heal_loop.py's own _run_sweep cleanup -- see its comment for why
            # this is needed (run_membership_reconcile_sweep sets app.tenant_id via plain SET,
            # not SET LOCAL, and the enqueue's commit makes that durable on the physical
            # connection; SessionLocal() bypasses get_db()'s own finally-block reset).
            db.rollback()
            db.execute(text("RESET app.tenant_id"))
            db.execute(text("RESET app.user_id"))
            db.commit()


async def membership_reconcile_loop() -> None:
    log.info("membership-reconcile sweep loop started")
    while True:
        try:
            await asyncio.to_thread(_run_sweep)
        except Exception:
            # A single sweep's exception must never kill the loop -- same posture as
            # gap_heal_loop.py's own outer except-Exception.
            log.exception("membership-reconcile sweep iteration failed")
        await asyncio.sleep(_read_poll_seconds())
