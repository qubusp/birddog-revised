#!/bin/bash
# marmot_reader: the read-only role the Marmot data catalog (apps box,
# deploy/marmot/README.md) crawls the birddog database with.
#
# Not birddog_reader: Marmot's Postgres plugin reads every database's size, which
# needs pg_read_all_stats (it also shows other sessions' query text in
# pg_stat_activity, so the notebook role should not have it).
#
# Runs as postgres when the volume is first created, and again with
# sh/deploy-data --init-roles:
#   docker compose exec -T -e MARMOT_READER_DB_PASSWORD postgres \
#       bash /docker-entrypoint-initdb.d/06-marmot-reader.sh
# Unlike the other roles, the grants live here and not in a migration:
# sh/deploy-data runs migrations before --init-roles, so a grant migration for a
# new role would be recorded as applied while still a no-op. Idempotent; also
# re-syncs the password.
set -euo pipefail

if [ -z "${MARMOT_READER_DB_PASSWORD:-}" ]; then
    echo "06-marmot-reader.sh: MARMOT_READER_DB_PASSWORD not set; skipping (sh/deploy-data --init-roles re-runs it)"
    exit 0
fi

psql -v ON_ERROR_STOP=1 -q --username postgres --dbname postgres \
     -v pw="$MARMOT_READER_DB_PASSWORD" <<'EOSQL'
DO $$ BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'marmot_reader') THEN
        CREATE ROLE marmot_reader LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT;
    END IF;
END $$;
ALTER ROLE marmot_reader PASSWORD :'pw';
ALTER ROLE marmot_reader SET default_transaction_read_only = on;
ALTER ROLE marmot_reader SET idle_in_transaction_session_timeout = '5min';
ALTER ROLE marmot_reader SET statement_timeout = '5min';
ALTER ROLE marmot_reader CONNECTION LIMIT 5;
GRANT pg_read_all_stats TO marmot_reader;
EOSQL

# the birddog database may not exist yet on a brand-new volume (01 creates it)
psql -v ON_ERROR_STOP=1 -q --username postgres --dbname birddog <<'EOSQL'
DO $$
DECLARE s text;
BEGIN
    FOREACH s IN ARRAY ARRAY['core', 'raw', 'views'] LOOP
        IF EXISTS (SELECT FROM pg_namespace WHERE nspname = s) THEN
            EXECUTE format('GRANT USAGE ON SCHEMA %I TO marmot_reader', s);
            EXECUTE format('GRANT SELECT ON ALL TABLES IN SCHEMA %I TO marmot_reader', s);
            EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE birddog IN SCHEMA %I GRANT SELECT ON TABLES TO marmot_reader', s);
        END IF;
    END LOOP;
    -- nothing in app (user accounts and other user-facing state)
    IF EXISTS (SELECT FROM pg_namespace WHERE nspname = 'app') THEN
        EXECUTE 'REVOKE ALL ON ALL TABLES IN SCHEMA app FROM marmot_reader';
        EXECUTE 'REVOKE ALL ON SCHEMA app FROM marmot_reader';
    END IF;
END $$;
EOSQL
