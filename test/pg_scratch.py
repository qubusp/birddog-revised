"""Throwaway-database support for the Postgres tests.

Set BIRDDOG_TEST_DATABASE_URL to a scratch database (its name must contain
"test"); the tests apply deploy/db/migrations/*.sql to it and point
BIRDDOG_DATABASE_URL at it. Without the variable they are skipped."""

import os
import unittest
from pathlib import Path
from urllib.parse import urlparse

TEST_URL_VAR = "BIRDDOG_TEST_DATABASE_URL"
MIGRATIONS = Path(__file__).resolve().parent.parent / "deploy" / "db" / "migrations"

_prepared = False


def test_database_url():
    return os.environ.get(TEST_URL_VAR)


def require_test_database():
    """Skip unless a scratch DB is configured; apply migrations once; return its URL."""
    global _prepared
    url = test_database_url()
    if not url:
        raise unittest.SkipTest(f"{TEST_URL_VAR} not set")
    if "test" not in (urlparse(url).path or ""):
        raise RuntimeError(f"{TEST_URL_VAR} must name a scratch database containing 'test'")
    os.environ["BIRDDOG_DATABASE_URL"] = url
    from birddog import app_db
    app_db.reset_pool()
    if not _prepared:
        import psycopg2
        conn = psycopg2.connect(url)
        try:
            with conn, conn.cursor() as cur:
                for path in sorted(MIGRATIONS.glob("*.sql")):
                    cur.execute(path.read_text(encoding="utf8"))
        finally:
            conn.close()
        _prepared = True
    return url


def truncate_app_tables():
    from birddog import app_db
    with app_db.cursor() as cur:
        cur.execute("""TRUNCATE app.user_account, app.user_preference, app.watch, app.alert_state,
                       app.kv, app.string_queue, app.request_log, app.service_log
                       RESTART IDENTITY CASCADE""")
