import os

from psycopg_pool import ConnectionPool

_pool: ConnectionPool | None = None


def get_pool() -> ConnectionPool:
    """Return the process-wide connection pool (singleton).

    Reads DATABASE_URL from the environment (injected via `bws run`).
    Raises RuntimeError if DATABASE_URL is not set.
    """
    global _pool
    if _pool is not None:
        return _pool
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise RuntimeError(
            "DATABASE_URL is not set; run the command under `bws run` "
            "so Bitwarden injects the connection string"
        )
    _pool = ConnectionPool(conninfo=dsn, min_size=1, max_size=4, open=True)
    return _pool
