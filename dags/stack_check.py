"""Smoke test for the stack: the birddog library imports inside Airflow, and
the birddog database is reachable with its schemas in place. Trigger by hand."""

from datetime import datetime

from airflow.sdk import dag, task


@dag(schedule=None, start_date=datetime(2026, 11, 1), catchup=False, tags=["ops"])
def stack_check():

    @task
    def birddog_library():
        from birddog.wiki import canonicalize_title
        return canonicalize_title("Архів:ДАЗО/1")

    @task
    def birddog_database():
        from airflow.providers.postgres.hooks.postgres import PostgresHook
        rows = PostgresHook(postgres_conn_id="birddog_pg").get_records(
            "select schema_name from information_schema.schemata "
            "where schema_name in ('raw', 'core', 'app') order by 1"
        )
        schemas = [r[0] for r in rows]
        assert schemas == ["app", "core", "raw"], schemas
        return schemas

    birddog_library()
    birddog_database()


stack_check()
