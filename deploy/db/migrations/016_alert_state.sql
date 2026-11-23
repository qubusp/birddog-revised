-- Web app state and grants (birddog-web).

-- birddog_web calls core.search() (the /search endpoint); 008 granted it to
-- the other roles only, so the web app depended on the PUBLIC default
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'birddog_web') THEN
        RAISE NOTICE 'role birddog_web missing; run deploy/db/init/05-web.sh then re-run migrations';
        RETURN;
    END IF;
    EXECUTE 'GRANT EXECUTE ON FUNCTION core.search(text, int) TO birddog_web';
END $$;
