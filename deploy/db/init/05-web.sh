#!/bin/bash
# birddog_web: the role the Flask app (birddog-web, deploy/web/README.md)
# connects as.
#
# Runs as postgres when the volume is first created, and again with
# sh/deploy-data --init-roles (role creation needs CREATEROLE, which birddog lacks):
#   docker compose exec -T -e BIRDDOG_WEB_DB_PASSWORD postgres \
#       bash /docker-entrypoint-initdb.d/05-web.sh
# The password is passed on the exec rather than in the postgres service's
# environment so adding it never forces a Postgres restart. Idempotent; also
# re-syncs the password. Grants live in migrations/012_web_role.sql.
set -euo pipefail

if [ -z "${BIRDDOG_WEB_DB_PASSWORD:-}" ]; then
    echo "05-web.sh: BIRDDOG_WEB_DB_PASSWORD not set; skipping (sh/deploy-data --init-roles re-runs it)"
    exit 0
fi

psql -v ON_ERROR_STOP=1 -q --username postgres --dbname postgres \
     -v web_pw="$BIRDDOG_WEB_DB_PASSWORD" <<'EOSQL'
DO $$ BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'birddog_web') THEN
        CREATE ROLE birddog_web LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
    END IF;
END $$;
ALTER ROLE birddog_web PASSWORD :'web_pw';
-- one gunicorn process with a small pool; leave room for a second during a rolling restart
ALTER ROLE birddog_web CONNECTION LIMIT 40;
ALTER ROLE birddog_web SET idle_in_transaction_session_timeout = '5min';
EOSQL
