"""Nightly encrypted backup of the birddog database to Google Drive, and a
weekly check that the latest backup actually restores.

rclone reads deploy/rclone/rclone.conf (not committed), which must define a
`gdrive-crypt` remote: a crypt remote layered on a Google Drive remote. Set it
up once with `rclone config` (see deploy/rclone/README.md)."""

from datetime import datetime, timedelta

from airflow.sdk import dag, task

_START = datetime(2026, 11, 1)
_REMOTE = "gdrive-crypt:birddog-backups"
# copies older than this are pruned from each folder
_RETENTION = {"daily": "8d", "weekly": "35d", "monthly": "400d"}
_DEFAULTS = {"retries": 1, "retry_delay": timedelta(minutes=10)}


@dag(schedule="0 2 * * *", start_date=_START, catchup=False, max_active_runs=1,
     default_args=_DEFAULTS, tags=["ops"])
def backup_postgres():

    @task.bash
    def dump_and_upload():
        return f"""
        set -euo pipefail
        # the image also carries a newer client; dump with the server's major version
        export PATH=/usr/lib/postgresql/16/bin:$PATH
        name=birddog-$(date -u +%Y%m%dT%H%MZ).dump
        file=/tmp/$name
        pg_dump --format=custom --no-owner --dbname="$BIRDDOG_DATABASE_URL" --file="$file"
        rclone copyto "$file" {_REMOTE}/daily/$name
        # Sundays also go to weekly/, the 1st of the month to monthly/
        if [ "$(date -u +%u)" = 7 ]; then rclone copyto "$file" {_REMOTE}/weekly/$name; fi
        if [ "$(date -u +%d)" = 01 ]; then rclone copyto "$file" {_REMOTE}/monthly/$name; fi
        rm -f "$file"
        for folder in daily weekly monthly; do
            case $folder in daily) age={_RETENTION['daily']};; weekly) age={_RETENTION['weekly']};; monthly) age={_RETENTION['monthly']};; esac
            rclone delete --min-age $age {_REMOTE}/$folder || true
        done
        echo "uploaded $name"
        """

    dump_and_upload()


@dag(schedule="0 4 * * 0", start_date=_START, catchup=False, max_active_runs=1,
     default_args=_DEFAULTS, tags=["ops"])
def verify_restore():

    @task.bash
    def restore_latest():
        # restores into a scratch database next to the live one, as the
        # postgres superuser (the birddog role can't create databases)
        return f"""
        set -euo pipefail
        export PATH=/usr/lib/postgresql/16/bin:$PATH
        latest=$(rclone lsf --files-only {_REMOTE}/daily | sort | tail -1)
        [ -n "$latest" ] || {{ echo "no backups found"; exit 1; }}
        rclone copyto {_REMOTE}/daily/$latest /tmp/restore.dump
        export PGHOST=postgres PGUSER=postgres PGPASSWORD="$POSTGRES_PASSWORD"
        dropdb --if-exists birddog_restore_check
        createdb birddog_restore_check
        pg_restore --no-owner --exit-on-error --dbname=birddog_restore_check /tmp/restore.dump
        rm -f /tmp/restore.dump
        echo "$latest"
        """

    @task
    def compare_counts(backup_name):
        """Fails when a table has fewer rows in the restored copy than allowed
        for a day's growth, or is missing from it."""
        import os
        import psycopg2

        def counts(dbname):
            conn = psycopg2.connect(host="postgres", user="postgres",
                                    password=os.environ["POSTGRES_PASSWORD"], dbname=dbname)
            with conn, conn.cursor() as cur:
                cur.execute("""SELECT schemaname || '.' || relname FROM pg_stat_user_tables
                               WHERE schemaname IN ('raw', 'core', 'app')""")
                tables = [r[0] for r in cur.fetchall()]
                result = {}
                for table in tables:
                    cur.execute(f"SELECT count(*) FROM {table}")
                    result[table] = cur.fetchone()[0]
            conn.close()
            return result

        live, restored = counts("birddog"), counts("birddog_restore_check")
        problems = []
        for table, n in live.items():
            got = restored.get(table)
            # the backup is up to a week old, so allow it to be behind by 10%
            if got is None or got < n * 0.9:
                problems.append(f"{table}: live {n}, restored {got}")
        if problems:
            raise ValueError(f"restore check of {backup_name} failed: " + "; ".join(problems))
        return {"backup": backup_name, "tables": len(live)}

    @task.bash(trigger_rule="all_done")
    def drop_scratch():
        return ("PGHOST=postgres PGUSER=postgres PGPASSWORD=\"$POSTGRES_PASSWORD\" "
                "dropdb --if-exists birddog_restore_check")

    name = restore_latest()
    compare_counts(name) >> drop_scratch()


backup_postgres()
verify_restore()
