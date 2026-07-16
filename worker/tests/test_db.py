import os

import pytest

from notbulk.db import get_pool


def test_get_pool_raises_without_database_url(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    # reset the module-level singleton so the missing-env path is exercised
    import notbulk.db as dbmod
    dbmod._pool = None
    with pytest.raises(RuntimeError):
        get_pool()


@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set (run under `bws run` for the integration path)",
)
def test_get_pool_select_one_and_is_singleton():
    import notbulk.db as dbmod
    dbmod._pool = None
    pool = get_pool()
    assert get_pool() is pool  # singleton
    with pool.connection() as conn:
        row = conn.execute("SELECT 1").fetchone()
    assert row[0] == 1
