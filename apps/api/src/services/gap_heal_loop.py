"""S5 PR 2: asyncio background loop that periodically runs the gap-heal sweep.

Mirrors apps/worker/src/worker.py's run()'s own while-True-plus-sleep poll loop, in the API
process instead: the API is already a long-running uvicorn process (unlike a request-scoped
handler), so this is the smallest thing that satisfies "runs periodically" without pulling in
APScheduler/Celery/cron or a new container. Started from src.main's lifespan; a request-scoped
DB session can't be reused here (its lifetime is the request, not this loop), so each iteration
opens and closes its own short-lived session.
"""

import asyncio
import logging

from sqlalchemy import text

from src.core.app_config import get_config
from src.core.db import SessionLocal
from src.services.gap_heal_sweep import run_gap_heal_sweep

log = logging.getLogger(__name__)

# Same reasoning as worker.py's _MAX_POLL_SECONDS -- an upper clamp so a misconfigured
# app_config value can't make the loop effectively never run.
_MAX_POLL_SECONDS = 3600
_DEFAULT_POLL_SECONDS = 900


def _read_poll_seconds() -> int:
    raw = get_config("gap_heal_poll_seconds", str(_DEFAULT_POLL_SECONDS))
    try:
        return max(60, min(_MAX_POLL_SECONDS, int(raw)))
    except ValueError:
        log.warning("gap_heal_poll_seconds %r is not an integer; using %d", raw, _DEFAULT_POLL_SECONDS)
        return _DEFAULT_POLL_SECONDS


def _run_sweep() -> None:
    with SessionLocal() as db:
        try:
            run_gap_heal_sweep(db)
        finally:
            # Mirrors get_db()'s own cleanup (src/core/db.py): run_gap_heal_sweep sets
            # app.tenant_id via plain SET (not SET LOCAL) once per tenant, and
            # backfill_service.enqueue's db.commit() makes that SET durable on the
            # physical connection, not just this Session. SessionLocal() here bypasses
            # get_db()'s own finally-block reset, so without this, the connection pool
            # would hand the next checkout (a real request via get_db(), or the next
            # sweep iteration) a connection with a leftover tenant context.
            db.rollback()
            db.execute(text("RESET app.tenant_id"))
            db.execute(text("RESET app.user_id"))
            db.commit()


async def gap_heal_loop() -> None:
    log.info("gap-heal sweep loop started")
    while True:
        try:
            await asyncio.to_thread(_run_sweep)
        except Exception:
            # A single sweep's exception must never kill the loop -- same posture as
            # worker.py's run()'s own outer except Exception around each poll iteration.
            log.exception("gap-heal sweep iteration failed")
        await asyncio.sleep(_read_poll_seconds())
