-- app: the web app's own state on Postgres, replacing DynamoDB/SQLite/S3
-- (birddog/store.py, birddog/log.py, birddog/user.py; selected by
-- birddog.env.store_backend()).
--   accounts     app.user_account   (was S3 users/<email>.json)
--   watchlists   app.watch          (was KV namespace "wl:<email>")
--   preferences  app.user_preference (was KV namespace "pref:<email>")
--   everything else that used KeyValueStore/StringQueue (watcher state,
--   tracker/updater state, ...) goes to the generic app.kv / app.string_queue,
--   one "store" per former DynamoDB table / SQLite file.
-- birddog/dynamo_migrate.py copies the existing AWS data in.

-- accounts: password_hash keeps the werkzeug hash string as stored on S3
-- (e.g. "scrypt:32768:8:1$salt$hash"), so existing passwords keep working
ALTER TABLE app.user_account ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now();

-- watchlist entries: a new watch has no last check yet, and legacy entries
-- may lack a cutoff
ALTER TABLE app.watch ALTER COLUMN last_checked_date DROP NOT NULL;
ALTER TABLE app.watch ALTER COLUMN cutoff_date DROP NOT NULL;
ALTER TABLE app.watch ALTER COLUMN include SET DEFAULT '{}';

-- generic key-value store (AbstractKeyValueStore); value is the JSON/text
-- string callers already serialize
CREATE TABLE IF NOT EXISTS app.kv (
    store      text NOT NULL,              -- former table name, e.g. bd_watchers
    namespace  text NOT NULL,
    key        text NOT NULL,
    value      text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (store, namespace, key)
);

-- leased string queues (AbstractStringQueue)
CREATE TABLE IF NOT EXISTS app.string_queue (
    id             bigserial PRIMARY KEY,
    store          text   NOT NULL,
    queue_name     text   NOT NULL,
    value          text   NOT NULL,
    lease_until_ms bigint,
    lease_owner    text,
    claimed_at_ms  bigint
);
CREATE INDEX IF NOT EXISTS string_queue_fifo ON app.string_queue (store, queue_name, id);

-- per-request log (EventLogger); user_id is the anonymized hash the app logs
CREATE TABLE IF NOT EXISTS app.request_log (
    id          bigserial PRIMARY KEY,
    ts          timestamptz NOT NULL DEFAULT now(),
    user_id     text,
    method      text,
    path        text,
    status      int,
    duration_ms int,
    source_key  text                       -- set for rows copied from DynamoDB
);
CREATE INDEX IF NOT EXISTS request_log_ts ON app.request_log (ts);
CREATE UNIQUE INDEX IF NOT EXISTS request_log_source_key ON app.request_log (source_key);

-- per-call log of external services (ServiceLogger / LogService), read by the
-- kill switch and /service_usage
CREATE TABLE IF NOT EXISTS app.service_log (
    id          bigserial PRIMARY KEY,
    ts          timestamptz NOT NULL DEFAULT now(),
    resource    text,
    method      text,
    path        text,
    size        bigint,
    duration_ms int,
    source_key  text
);
CREATE INDEX IF NOT EXISTS service_log_ts ON app.service_log (ts);
CREATE UNIQUE INDEX IF NOT EXISTS service_log_source_key ON app.service_log (source_key);
