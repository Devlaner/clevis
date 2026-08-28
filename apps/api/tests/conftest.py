import logging
from urllib.parse import urlsplit

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from src.core.config import settings
from src.core.db import Base

logger = logging.getLogger(__name__)

# Hosts a throwaway local/CI Postgres is actually reachable at in this repo's own dev/CI setup
# (docker-compose's `db` service, or GitHub Actions' service-container port-forward to
# localhost) -- never a managed/hosted Postgres a real deployment would use. This is the
# fail-closed guard CodeRabbit asked for on the truncate below: an allowlist, not a denylist,
# so an unrecognized host (e.g. a real deployment's DB) is refused by default rather than
# trusted by default.
_DISPOSABLE_DB_HOSTS = {"localhost", "127.0.0.1", "db"}


@pytest.fixture(scope="session")
def _engine():
    eng = create_engine(settings.database_url.get_secret_value())
    Base.metadata.create_all(eng)  # no-op if alembic already ran
    yield eng
    # Per-test isolation above never leaves committed rows -- but this DB is also the one a
    # developer points a manually-run `uvicorn`/E2E session at (see "Running locally" in
    # AGENTS.md), and *that* traffic commits for real. A leftover workspace-admin user from a
    # prior manual session makes /auth/setup-dependent tests fail with 409 instead of 201 on the
    # next `pytest -q` run. Truncating every ORM table once at the very end of the whole session
    # (not per-test -- that would defeat the fast savepoint-rollback approach above) guarantees
    # the next run starts clean regardless of what non-test traffic touched this DB in between.
    host = urlsplit(settings.database_url.get_secret_value()).hostname
    if host not in _DISPOSABLE_DB_HOSTS:
        logger.warning("skipping post-session DB truncate: %r is not a recognized disposable-DB host", host)
        return

    # Best-effort, not fatal: CI additionally runs this suite against the constrained
    # `clevis_api`/`clevis_worker` roles (to verify RLS enforcement) which are granted
    # SELECT/INSERT/UPDATE/DELETE but not TRUNCATE (migration 0032 and friends never grant it,
    # deliberately -- this cleanup is a local-dev convenience, not something production roles
    # need). CI's Postgres containers are ephemeral per run anyway, so there's nothing to clean
    # up there; only swallow the expected privilege error, not real bugs.
    try:
        with eng.begin() as conn:
            # Base.metadata.tables (unordered), not .sorted_tables -- a topological sort isn't
            # needed since TRUNCATE ... CASCADE handles FK ordering itself, and orgs/tenants have
            # a genuine FK cycle between them that makes .sorted_tables raise a SAWarning.
            table_names = [table.name for table in Base.metadata.tables.values()]
            if table_names:
                quoted = ", ".join(f'"{name}"' for name in table_names)
                conn.execute(text(f"TRUNCATE TABLE {quoted} RESTART IDENTITY CASCADE"))
    except DBAPIError as exc:
        if "InsufficientPrivilege" not in type(exc.orig).__name__:
            raise
        logger.warning("skipping post-session DB truncate: connected role lacks TRUNCATE privilege")


@pytest.fixture
def db(_engine):
    with _engine.connect() as conn:
        conn.begin()
        with Session(conn, join_transaction_mode="create_savepoint") as session:
            yield session
        conn.rollback()
        # require_org_role/require_personal_tenant (rbac.py) set app.tenant_id/app.user_id
        # via plain SET, not SET LOCAL -- rollback() above doesn't clear them. _engine is
        # session-scoped, so a pooled connection can be reused by a later test; reset here
        # the same way get_db() does in production, or a leaked value leaks across tests.
        conn.execute(text("RESET app.tenant_id"))
        conn.execute(text("RESET app.user_id"))
        # RESET is transactional like any other statement -- the execute() calls above
        # auto-begin a new implicit transaction after rollback() ended the last one, and
        # closing the connection without committing would roll the resets themselves back,
        # leaving the leaked value intact on the pooled connection. Must commit for the
        # reset to actually stick.
        conn.commit()
