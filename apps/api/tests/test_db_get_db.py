"""Regression test for get_db()'s tenant-session-context reset on connection checkin
(issue #190, PR 5a). require_org_role/require_personal_tenant set app.tenant_id/
app.user_id via plain SET (not SET LOCAL, since a request can commit more than once) --
SET persists for the life of the physical connection, not just one request's Session, so
without an explicit reset it would leak into whatever unrelated request reuses the same
pooled connection next. Uses the real engine/pool directly (not the savepoint-per-test
`db` fixture), since the bug is specifically about connection *reuse* across separate
get_db() calls."""

from unittest.mock import patch

from sqlalchemy import text
from sqlalchemy.orm import Session

from src.core.db import get_db


def _current_setting_via_new_connection() -> str | None:
    gen = get_db()
    db = next(gen)
    try:
        value = db.execute(text("SELECT current_setting('app.tenant_id', true)")).scalar()
    finally:
        gen.close()
    return value or None


def test_get_db_resets_tenant_context_before_connection_checkin():
    gen = get_db()
    db = next(gen)
    db.execute(text("SET app.tenant_id = 555555"))
    db.execute(text("SET app.user_id = 777777"))
    db.commit()
    assert db.execute(text("SELECT current_setting('app.tenant_id', true)")).scalar() == "555555"
    gen.close()  # triggers get_db()'s finally: block

    # A fresh get_db() call may or may not reuse the exact same physical connection
    # depending on pool state, so poll a handful of connections -- if the reset didn't
    # happen, the leaked value would show up on at least one of them (LIFO pools strongly
    # favor immediate reuse of the just-returned connection in a single-threaded test).
    seen = {_current_setting_via_new_connection() for _ in range(5)}
    assert seen == {None}, f"app.tenant_id leaked into a reused connection: {seen}"


def test_get_db_invalidates_the_connection_if_the_reset_itself_fails():
    # If RESET/commit fails partway through, closing normally would return a connection
    # to the pool whose reset status is uncertain -- get_db() must invalidate it instead
    # of risking a dirty connection reaching an unrelated later request.
    original_commit = Session.commit
    calls = {"n": 0}

    def failing_commit(self):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated failure during tenant-context reset")
        return original_commit(self)

    gen = get_db()
    next(gen)
    with patch.object(Session, "commit", failing_commit), patch.object(Session, "invalidate") as mock_invalidate:
        gen.close()

    mock_invalidate.assert_called_once()
