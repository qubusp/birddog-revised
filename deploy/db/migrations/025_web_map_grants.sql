-- The Map page in the web app (birddog/place_map.py) reads the same map views
-- Grafana does. birddog_web had only the Confirm places views (024); this lets
-- it read views.map__locations and views.map__book_places too.
--
-- 021 drops and recreates both views, which loses this grant; migrations run in
-- order, so a full re-run grants it again here. Idempotent; tolerates a missing role.

DO $$
BEGIN
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'birddog_web') THEN
        EXECUTE 'GRANT USAGE ON SCHEMA views TO birddog_web';
        EXECUTE 'GRANT SELECT ON views.map__locations, views.map__book_places TO birddog_web';
    END IF;
END $$;
