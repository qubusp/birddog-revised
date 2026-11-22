#!/bin/bash
# Roles and database for the curator UI (Mathesar, see deploy/ui/README.md).
#
# Runs as postgres when the volume is first created (after 01-databases.sh),
# and again on every deploy via sh/deploy:
#   docker compose exec -T postgres bash /docker-entrypoint-initdb.d/02-ui.sh
# because role creation needs CREATEROLE, which the migration role (birddog)
# doesn't have. Idempotent; also re-syncs the passwords from deploy/.env.
# Grants on the birddog schemas live in migrations/004_ui_role.sql.
set -euo pipefail

: "${BIRDDOG_UI_DB_PASSWORD:?}" "${MATHESAR_DB_PASSWORD:?}"

psql -v ON_ERROR_STOP=1 -q --username postgres --dbname postgres \
     -v ui_pw="$BIRDDOG_UI_DB_PASSWORD" -v msar_pw="$MATHESAR_DB_PASSWORD" <<'EOSQL'
-- birddog_ui: what curators act as inside the birddog database
DO $$ BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'birddog_ui') THEN
        CREATE ROLE birddog_ui LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;
    END IF;
END $$;
ALTER ROLE birddog_ui PASSWORD :'ui_pw';

-- mathesar: owns Mathesar's own metadata database (users, settings)
DO $$ BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'mathesar') THEN
        CREATE ROLE mathesar LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;
    END IF;
END $$;
ALTER ROLE mathesar PASSWORD :'msar_pw';

SELECT 'CREATE DATABASE mathesar OWNER mathesar'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'mathesar') \gexec

-- only the metadata owner may connect to the metadata database
REVOKE CONNECT ON DATABASE mathesar FROM PUBLIC;
EOSQL
