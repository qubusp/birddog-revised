-- Privileges for birddog_web, the role the Flask app (birddog-web service)
-- connects as. The role itself is created by deploy/db/init/05-web.sh (needs
-- CREATEROLE, which birddog lacks); until it exists this file is a no-op.
--
-- The app owns the user-facing state in app.*, reads core.* (pages,
-- documents, search) and maintains the archive-root registry. Everything else
-- in core is written by the Airflow pipeline (as birddog); raw is not needed.
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'birddog_web') THEN
        RAISE NOTICE 'role birddog_web missing; run deploy/db/init/05-web.sh then re-run migrations';
        RETURN;
    END IF;

    EXECUTE 'GRANT USAGE ON SCHEMA core, app TO birddog_web';

    EXECUTE 'GRANT SELECT ON ALL TABLES IN SCHEMA core TO birddog_web';   -- includes views
    EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON core.archive_root TO birddog_web';

    EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA app TO birddog_web';
    EXECUTE 'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA app TO birddog_web';

    -- tables birddog creates later (migrations) get the same rights
    EXECUTE 'ALTER DEFAULT PRIVILEGES FOR ROLE birddog IN SCHEMA core GRANT SELECT ON TABLES TO birddog_web';
    EXECUTE 'ALTER DEFAULT PRIVILEGES FOR ROLE birddog IN SCHEMA app GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO birddog_web';
    EXECUTE 'ALTER DEFAULT PRIVILEGES FOR ROLE birddog IN SCHEMA app GRANT USAGE, SELECT ON SEQUENCES TO birddog_web';

    -- explicit: nothing in raw
    EXECUTE 'REVOKE ALL ON ALL TABLES IN SCHEMA raw FROM birddog_web';
    EXECUTE 'REVOKE ALL ON SCHEMA raw FROM birddog_web';
END $$;
