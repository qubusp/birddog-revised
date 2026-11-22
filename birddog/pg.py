"""Postgres access for the birddog database.

BIRDDOG_DATABASE_URL (a postgresql:// URL) selects Postgres. When it isn't set,
callers fall back to their legacy stores, so the old runtime keeps working
during the migration."""

import os
from contextlib import contextmanager

_DATABASE_URL_VAR = "BIRDDOG_DATABASE_URL"


def database_url():
    return os.environ.get(_DATABASE_URL_VAR)


def enabled():
    return bool(database_url())


@contextmanager
def connect():
    """Yields a connection; commits on success, rolls back on error."""
    import psycopg2
    conn = psycopg2.connect(database_url())
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
