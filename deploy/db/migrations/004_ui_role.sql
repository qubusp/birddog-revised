-- Privileges for birddog_ui, the role the curator UI (Mathesar) connects as.
-- The role itself is created by deploy/db/init/02-ui.sh (needs CREATEROLE,
-- which birddog lacks); until it exists this file is a no-op.
--
-- Curators may read/write the curated core tables, read the rest of core
-- (caches, FTP listing, work-queue views), and nothing in app or raw.
DO $$
DECLARE
    t text;
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'birddog_ui') THEN
        RAISE NOTICE 'role birddog_ui missing; run deploy/db/init/02-ui.sh then re-run migrations';
        RETURN;
    END IF;

    EXECUTE 'GRANT USAGE ON SCHEMA core TO birddog_ui';
    EXECUTE 'GRANT SELECT ON ALL TABLES IN SCHEMA core TO birddog_ui';   -- includes views

    FOREACH t IN ARRAY ARRAY[
        'page', 'document', 'location', 'archive_root',
        'page_document', 'document_location', 'document_duplicate'
    ] LOOP
        EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON core.%I TO birddog_ui', t);
    END LOOP;
    EXECUTE 'GRANT USAGE, SELECT ON SEQUENCE core.page_id_seq, core.document_id_seq TO birddog_ui';

    -- explicit: no access to user-facing state or raw source data
    EXECUTE 'REVOKE ALL ON ALL TABLES IN SCHEMA app FROM birddog_ui';
    EXECUTE 'REVOKE ALL ON SCHEMA app FROM birddog_ui';
    EXECUTE 'REVOKE ALL ON ALL TABLES IN SCHEMA raw FROM birddog_ui';
    EXECUTE 'REVOKE ALL ON SCHEMA raw FROM birddog_ui';
END $$;
