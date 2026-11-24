-- Book reading: Claude reads the page images of multi-page documents (books)
-- and lists the places they name. DAG: dags/yaml/books.yml; code:
-- birddog/books.py. Pilot scope: vital-record PDFs on Commons.
--
-- core.book_read is the work queue, one row per book:
--   pending         queued by the DAG's enqueue task
--   unreadable      Haiku's probe found the scans unusable (stop)
--   needs_approval  estimated tokens above the DAG's auto limit: a person
--                   sets approved or rejected in Mathesar (birddog_ui may
--                   update status and approval_note, nothing else)
--   approved        read it (set by the probe or by a person)
--   rejected        a person said no (stop)
--   reading         extraction under way (pages land in core.book_page)
--   read            every page extracted; waiting for assembly
--   done            places assembled into `places`
--   failed          see `error`; set back to approved (or pending) to retry
-- Pages are stored as they finish, so an interrupted book resumes where it
-- stopped. Nothing here writes core.document_location yet: the pilot's
-- results are checked by hand first.
--
-- Idempotent (re-run by every scratch test database and --all-migrations).

CREATE TABLE IF NOT EXISTS core.book_read (
    document_id     bigint PRIMARY KEY REFERENCES core.document(id) ON DELETE CASCADE,
    status          text NOT NULL DEFAULT 'pending',
    page_count      integer,
    probe           jsonb,            -- Haiku's verdict on sample pages
    tokens_per_page integer,          -- measured on the probe's images
    est_tokens      bigint,           -- estimate for the whole book (input + output)
    est_cost_usd    numeric(10, 2),   -- the same estimate in dollars at extraction prices
    input_tokens    bigint NOT NULL DEFAULT 0,
    output_tokens   bigint NOT NULL DEFAULT 0,
    cost_usd        numeric(10, 4) NOT NULL DEFAULT 0,
    places          jsonb,            -- assembled result (Haiku + matcher)
    approval_note   text,             -- free text for whoever approves or rejects
    error           text,
    run_id          text,             -- Airflow run that last worked on it
    queued_at       timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT book_read_status_check CHECK (status IN (
        'pending', 'unreadable', 'needs_approval', 'approved', 'rejected',
        'reading', 'read', 'done', 'failed'))
);
CREATE INDEX IF NOT EXISTS book_read_status_idx ON core.book_read (status);
COMMENT ON TABLE core.book_read IS
    'Book reading queue and results (dags/yaml/books.yml). Approve over-limit books by setting status to approved or rejected.';

CREATE TABLE IF NOT EXISTS core.book_page (
    document_id   bigint NOT NULL REFERENCES core.book_read(document_id) ON DELETE CASCADE,
    page_no       integer NOT NULL,
    model         text NOT NULL,      -- the model that produced this reading
    illegible     boolean NOT NULL DEFAULT false,
    result        jsonb NOT NULL,     -- {"places": [...], "notes": ...}
    input_tokens  integer NOT NULL DEFAULT 0,
    output_tokens integer NOT NULL DEFAULT 0,
    cost_usd      numeric(10, 4) NOT NULL DEFAULT 0,
    created_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (document_id, page_no, model)
);
COMMENT ON TABLE core.book_page IS
    'Per-page extraction results of core.book_read; a page re-read by a stronger model gets a second row.';

-- keep updated_at honest whoever edits the row (the DAG or a curator)
CREATE OR REPLACE FUNCTION core.book_read_touch() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS book_read_touch ON core.book_read;
CREATE TRIGGER book_read_touch BEFORE UPDATE ON core.book_read
    FOR EACH ROW EXECUTE FUNCTION core.book_read_touch();

-- Mathesar shows views read-only: this one lists what waits for a decision;
-- edit status in core.book_read (same document_id) to decide.
CREATE OR REPLACE VIEW views.books__needs_approval AS
SELECT b.document_id,
       d.title,
       d.url,
       b.page_count,
       b.est_tokens,
       b.est_cost_usd,
       b.probe ->> 'legibility'  AS legibility,
       b.probe ->> 'script'      AS script,
       b.probe ->> 'languages'   AS languages,
       b.probe ->> 'notes'       AS probe_notes,
       b.approval_note,
       b.updated_at
FROM core.book_read b
JOIN core.document d ON d.id = b.document_id
WHERE b.status = 'needs_approval';
COMMENT ON VIEW views.books__needs_approval IS
    'Books over the auto-read limit. To decide, open core.book_read and set status to approved or rejected (sort: est_tokens).';

DO $$
BEGIN
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'birddog_ui') THEN
        EXECUTE 'GRANT SELECT ON core.book_read, core.book_page TO birddog_ui';
        EXECUTE 'GRANT UPDATE (status, approval_note) ON core.book_read TO birddog_ui';
        EXECUTE 'GRANT SELECT ON views.books__needs_approval TO birddog_ui';
    ELSE
        RAISE NOTICE 'role birddog_ui missing; run deploy/db/init/02-ui.sh then re-run migrations';
    END IF;
    -- birddog_reader and marmot_reader get the core tables through default
    -- privileges; marmot_reader's default privileges also cover `views`.
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'marmot_reader') THEN
        EXECUTE 'GRANT SELECT ON views.books__needs_approval TO marmot_reader';
    END IF;
END $$;
