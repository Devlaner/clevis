import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from src.core.config import settings
from src.core.db import Base


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
    with eng.begin() as conn:
        # Base.metadata.tables (unordered), not .sorted_tables -- a topological sort isn't
        # needed since TRUNCATE ... CASCADE handles FK ordering itself, and orgs/tenants have
        # a genuine FK cycle between them that makes .sorted_tables raise a SAWarning.
        table_names = [table.name for table in Base.metadata.tables.values()]
        if table_names:
            quoted = ", ".join(f'"{name}"' for name in table_names)
            conn.execute(text(f"TRUNCATE TABLE {quoted} RESTART IDENTITY CASCADE"))


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
