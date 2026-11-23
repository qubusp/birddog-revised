-- Source kit (birddog/sources, dags/sources/*/source.yml): one generic landing
-- table for every onboarded source, plus a log of each run. A source lands its
-- records here unchanged (payload as JSON), then its own transform SQL turns
-- them into core rows. See dags/sources/README.md.
--
-- raw.source_record keeps the latest copy of each record (like
-- raw.nocodb_record), not every version: a record is rewritten only when its
-- payload changed, and then carries the batch that changed it. The transform
-- step reads the records of the batches it hasn't processed yet, so a re-run
-- of an unchanged source touches nothing in core (and doesn't overwrite
-- curator edits made since).
--
-- Idempotent; the migrate service re-runs it on every deploy.

CREATE TABLE IF NOT EXISTS raw.source_run (
    batch_id        bigserial PRIMARY KEY,
    source          text NOT NULL,
    run_id          text,                    -- Airflow run id, or 'cli'
    status          text NOT NULL DEFAULT 'landing',
                    -- landing / landed / transformed / verified / failed
    full_fetch      boolean NOT NULL DEFAULT false,  -- fetched the whole source (gone records are meaningful)
    started_at      timestamptz NOT NULL DEFAULT now(),
    landed_at       timestamptz,
    transformed_at  timestamptz,             -- set when its records went through the transform
    verified_at     timestamptz,
    fetched         int,                     -- records the fetcher returned
    changed         int,                     -- of those, new or with a changed payload
    watermark_before text,
    watermark_after  text,
    checks          jsonb,                   -- verification results
    error           text
);
CREATE INDEX IF NOT EXISTS source_run_source ON raw.source_run (source, batch_id DESC);
-- the transform's work queue: runs whose records haven't been transformed
CREATE INDEX IF NOT EXISTS source_run_pending ON raw.source_run (source) WHERE transformed_at IS NULL;

CREATE TABLE IF NOT EXISTS raw.source_record (
    source          text   NOT NULL,
    key             text   NOT NULL,         -- the source's landing key (source.yml `key`)
    payload         jsonb  NOT NULL,         -- the record as fetched
    payload_hash    text   NOT NULL,         -- md5 of the canonical JSON, to skip unchanged records
    batch_id        bigint NOT NULL REFERENCES raw.source_run (batch_id),  -- batch that last changed it
    seen_batch_id   bigint NOT NULL REFERENCES raw.source_run (batch_id),  -- batch that last saw it
    first_landed_at timestamptz NOT NULL DEFAULT now(),
    landed_at       timestamptz NOT NULL DEFAULT now(),  -- when the payload last changed
    PRIMARY KEY (source, key)
);
CREATE INDEX IF NOT EXISTS source_record_batch ON raw.source_record (source, batch_id);
CREATE INDEX IF NOT EXISTS source_record_seen ON raw.source_record (source, seen_batch_id);

COMMENT ON TABLE raw.source_run IS
    'Source kit: one row per land/transform/verify run of a source (dags/sources/<name>).';
COMMENT ON TABLE raw.source_record IS
    'Source kit: latest landed copy of every record of every source, payload unchanged.';

-- Per-source watermarks live in raw.watermark under the name 'source:<name>'.

-- birddog_reader (JupyterHub) already reads all of raw through the default
-- privileges in 007_reader_role.sql; nothing to grant here. Curators
-- (birddog_ui) see sources only through the views.source__<name> views the
-- kit's expose step creates.
