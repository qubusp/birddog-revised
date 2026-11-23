#!/bin/bash
# birddog_reader: the read-only role JupyterHub notebooks connect as
# (see deploy/jupyterhub/README.md).
#
# Runs as postgres when the volume is first created, and again on every deploy
# via sh/deploy (role creation needs CREATEROLE, which birddog lacks):
#   docker compose exec -T -e JUPYTER_DB_PASSWORD postgres \
#       bash /docker-entrypoint-initdb.d/03-reader.sh
# The password is passed on the exec rather than in the postgres service's
# environment so adding it never forces a Postgres restart. Idempotent; also
# re-syncs the password. Grants live in migrations/007_reader_role.sql.
set -euo pipefail

if [ -z "${JUPYTER_DB_PASSWORD:-}" ]; then
    echo "03-reader.sh: JUPYTER_DB_PASSWORD not set; skipping (sh/deploy re-runs it)"
    exit 0
fi

psql -v ON_ERROR_STOP=1 -q --username postgres --dbname postgres \
     -v reader_pw="$JUPYTER_DB_PASSWORD" <<'EOSQL'
DO $$ BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'birddog_reader') THEN
        CREATE ROLE birddog_reader LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
    END IF;
END $$;
ALTER ROLE birddog_reader PASSWORD :'reader_pw';
-- belt and braces on top of SELECT-only grants
ALTER ROLE birddog_reader SET default_transaction_read_only = on;
-- notebooks leave transactions open; don't let them hold locks that block
-- the import/migration DDL for long
ALTER ROLE birddog_reader SET idle_in_transaction_session_timeout = '10min';
ALTER ROLE birddog_reader CONNECTION LIMIT 30;
EOSQL
