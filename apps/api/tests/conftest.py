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
