#!/bin/bash
# Metadata roles + databases for the low-code app builders (deploy/apps/README.md):
#   tooljet  -> databases tooljet (app metadata), tooljet_db (ToolJet Database),
#               tooljet_sample (ToolJet's sample datasource)
#   saltcorn -> database saltcorn
# The roles get no CREATEDB/CREATEROLE; ToolJet is told not to create databases
# (PG_DB_OWNER=false) nor per-workspace roles (TJDB_SQL_MODE_DISABLE=true).
# Neither role can connect to birddog; the apps reach Birddog data as birddog_ui.
#
# Runs as postgres when the volume is first created, and again on every deploy
# via sh/deploy:
#   docker compose exec -T -e TOOLJET_DB_PASSWORD -e SALTCORN_DB_PASSWORD postgres \
#       bash /docker-entrypoint-initdb.d/04-apps.sh
# Passwords come on the exec (not the postgres service env) so adding them never
# forces a Postgres restart. Idempotent; also re-syncs the passwords.
set -euo pipefail

if [ -z "${TOOLJET_DB_PASSWORD:-}" ] || [ -z "${SALTCORN_DB_PASSWORD:-}" ]; then
    echo "04-apps.sh: TOOLJET_DB_PASSWORD/SALTCORN_DB_PASSWORD not set; skipping (sh/deploy re-runs it)"
    exit 0
fi

psql -v ON_ERROR_STOP=1 -q --username postgres --dbname postgres \
     -v tj_pw="$TOOLJET_DB_PASSWORD" -v sc_pw="$SALTCORN_DB_PASSWORD" <<'EOSQL'
DO $$ BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'tooljet') THEN
        CREATE ROLE tooljet LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'saltcorn') THEN
        CREATE ROLE saltcorn LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;
    END IF;
END $$;
ALTER ROLE tooljet PASSWORD :'tj_pw';
ALTER ROLE saltcorn PASSWORD :'sc_pw';

SELECT 'CREATE DATABASE tooljet OWNER tooljet'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'tooljet') \gexec
SELECT 'CREATE DATABASE tooljet_db OWNER tooljet'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'tooljet_db') \gexec
SELECT 'CREATE DATABASE tooljet_sample OWNER tooljet'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'tooljet_sample') \gexec
SELECT 'CREATE DATABASE saltcorn OWNER saltcorn'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'saltcorn') \gexec

-- only each metadata owner may connect to its databases
REVOKE CONNECT ON DATABASE tooljet, tooljet_db, tooljet_sample, saltcorn FROM PUBLIC;
EOSQL

# extensions ToolJet's migrations want (pgcrypto, citext are trusted, but create
# them here so the migrations' IF NOT EXISTS is a no-op)
for db in tooljet tooljet_db; do
    psql -v ON_ERROR_STOP=1 -q --username postgres --dbname "$db" \
         -c 'CREATE EXTENSION IF NOT EXISTS pgcrypto; CREATE EXTENSION IF NOT EXISTS citext; CREATE EXTENSION IF NOT EXISTS "uuid-ossp";'
done
