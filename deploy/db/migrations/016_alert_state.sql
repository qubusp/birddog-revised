-- Watcher alert state in its own tables instead of the generic app.kv store
-- "bd_watchers" (birddog/alert_store.py, used by birddog/watcher.py on the
-- Postgres stack).
--
--   watcher header  app.watch.watcher (jsonb: version, include, exclude,
--                   cutoff_date, last_checked_date[, moved_to]); NULL until
--                   the first check. It is separate from the watchlist's own
--                   columns: the header's last_checked_date is the newest
--                   change seen, the watchlist's is the wall-clock check time.
--   per item        app.alert_state: entry = the unresolved entry (resolved =
--                   false) or '{}' (resolved = true); history = the item's
--                   resolve history, oldest first. A row with neither is
--                   deleted. modified, last_resolved, significance and
--                   sig_span are derived by a trigger, for queries only.
--
-- The existing app.kv rows are copied by the app on first use (see
-- alert_store.copy_kv_state()), not here: the migration runs before the new
-- code is deployed, and the old code keeps writing app.kv until then.

ALTER TABLE app.watch ADD COLUMN IF NOT EXISTS watcher jsonb;
ALTER TABLE app.alert_state ADD COLUMN IF NOT EXISTS history jsonb NOT NULL DEFAULT '[]';

-- watcher dates are UTC ISO strings, but legacy entries can hold anything;
-- a bad one becomes NULL rather than failing the write
CREATE OR REPLACE FUNCTION app.try_timestamptz(t text) RETURNS timestamptz
LANGUAGE plpgsql STABLE AS $$
BEGIN
    RETURN t::timestamptz;
EXCEPTION WHEN others THEN
    RETURN NULL;
END
$$;

CREATE OR REPLACE FUNCTION app.alert_state_derive() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    last_resolve jsonb := CASE WHEN jsonb_typeof(NEW.history) = 'array' THEN NEW.history -> -1 END;
BEGIN
    NEW.last_resolved := app.try_timestamptz(last_resolve ->> 'last_resolved');
    IF NEW.resolved THEN
        NEW.modified := app.try_timestamptz(last_resolve ->> 'modified');
        NEW.significance := NULL;
        NEW.sig_span := NULL;
    ELSE
        NEW.modified := app.try_timestamptz(NEW.entry ->> 'modified');
        NEW.significance := CASE
            WHEN NEW.entry -> 'new_page' = 'true'::jsonb THEN 'new_page'
            WHEN NEW.entry -> 'insignificant' = 'true'::jsonb THEN 'insignificant'
        END;
        NEW.sig_span := NEW.entry -> 'sig_span';
    END IF;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS alert_state_derive ON app.alert_state;
CREATE TRIGGER alert_state_derive BEFORE INSERT OR UPDATE ON app.alert_state
    FOR EACH ROW EXECUTE FUNCTION app.alert_state_derive();

-- the significance sweep lists every watch with a header
CREATE INDEX IF NOT EXISTS watch_has_watcher ON app.watch (email, archive_title) WHERE watcher IS NOT NULL;

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
