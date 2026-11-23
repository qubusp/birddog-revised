-- Privileges for birddog_reader, the read-only role JupyterHub notebooks use
-- (deploy/jupyterhub). The role is created by deploy/db/init/03-reader.sh
-- (needs CREATEROLE, which birddog lacks); until it exists this is a no-op.
--
-- Read everything in core and raw (tables and views, current and future);
-- nothing in app (user accounts and other user-facing state).
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'birddog_reader') THEN
        RAISE NOTICE 'role birddog_reader missing; run deploy/db/init/03-reader.sh then re-run migrations';
        RETURN;
    END IF;

    EXECUTE 'GRANT USAGE ON SCHEMA core, raw TO birddog_reader';
    EXECUTE 'GRANT SELECT ON ALL TABLES IN SCHEMA core, raw TO birddog_reader';  -- includes views
    EXECUTE 'GRANT SELECT ON ALL SEQUENCES IN SCHEMA core, raw TO birddog_reader';
    -- tables birddog creates later (migrations, DAGs) are readable too
    EXECUTE 'ALTER DEFAULT PRIVILEGES FOR ROLE birddog IN SCHEMA core, raw GRANT SELECT ON TABLES TO birddog_reader';
    EXECUTE 'ALTER DEFAULT PRIVILEGES FOR ROLE birddog IN SCHEMA core, raw GRANT SELECT ON SEQUENCES TO birddog_reader';

    -- explicit: no access to user-facing state
    EXECUTE 'REVOKE ALL ON ALL TABLES IN SCHEMA app FROM birddog_reader';
    EXECUTE 'REVOKE ALL ON SCHEMA app FROM birddog_reader';
END $$;
