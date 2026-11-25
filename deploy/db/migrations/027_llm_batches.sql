-- Message Batches API mode for read_books and transcribe_books (DAG param
-- use_batch; birddog/book_batches.py). A run submits each phase (Sonnet, then
-- the Opus re-read) as one or more batches, waits with a rescheduling sensor,
-- then collects the results.
--
--   core.llm_batch          one row per submitted batch: which DAG, run and
--                           phase, its estimate (checked against budget_usd
--                           before submitting) and its actual cost
--   core.llm_batch_request  one row per request: custom_id -> book and pages,
--                           its status and cost. Pages in a request of a batch
--                           not yet collected are not submitted again, so a
--                           run that dies after submit never pays twice.
--   views.books__batches    progress per batch
--
-- Idempotent (re-run by every scratch test database and --all-migrations).

CREATE TABLE IF NOT EXISTS core.llm_batch (
    batch_id        text PRIMARY KEY,      -- the API's msgbatch_... id
    dag             text NOT NULL,         -- read_books, transcribe_books
    run_id          text NOT NULL,
    phase           text NOT NULL,         -- first (Sonnet), reread (Opus)
    model           text NOT NULL,
    status          text NOT NULL DEFAULT 'submitted',
    request_count   integer NOT NULL,
    submitted_at    timestamptz NOT NULL DEFAULT now(),
    ended_at        timestamptz,
    collected_at    timestamptz,
    est_cost_usd    numeric(10, 4) NOT NULL DEFAULT 0,   -- at batch prices
    actual_cost_usd numeric(10, 4),                      -- at batch prices (+ any synchronous retry)
    error           text,
    CONSTRAINT llm_batch_status_check CHECK (status IN ('submitted', 'ended', 'collected', 'failed')),
    CONSTRAINT llm_batch_phase_check CHECK (phase IN ('first', 'reread'))
);
CREATE INDEX IF NOT EXISTS llm_batch_dag_status_idx ON core.llm_batch (dag, status);
COMMENT ON TABLE core.llm_batch IS
    'Message Batches submitted by read_books / transcribe_books (birddog/book_batches.py): submitted -> ended -> collected.';

CREATE TABLE IF NOT EXISTS core.llm_batch_request (
    batch_id      text NOT NULL REFERENCES core.llm_batch(batch_id) ON DELETE CASCADE,
    custom_id     text NOT NULL,
    document_id   bigint NOT NULL,
    page_nos      integer[] NOT NULL,
    model         text NOT NULL,
    phase         text NOT NULL,
    status        text NOT NULL DEFAULT 'submitted',
    est_cost_usd  numeric(10, 4) NOT NULL DEFAULT 0,
    input_tokens  integer,
    output_tokens integer,
    cost_usd      numeric(10, 4),
    error         text,
    updated_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (batch_id, custom_id),
    CONSTRAINT llm_batch_request_status_check CHECK (status IN (
        'submitted', 'succeeded', 'refused', 'failed', 'errored', 'expired', 'canceled'))
);
CREATE INDEX IF NOT EXISTS llm_batch_request_doc_idx ON core.llm_batch_request (document_id);
COMMENT ON TABLE core.llm_batch_request IS
    'Requests of core.llm_batch: custom_id -> book and pages; refused = retried synchronously with the fallbacks.';

CREATE OR REPLACE VIEW views.books__batches AS
SELECT b.batch_id,
       b.dag,
       b.phase,
       b.model,
       b.status,
       b.run_id,
       b.request_count,
       count(r.*) FILTER (WHERE r.status = 'succeeded')                         AS succeeded,
       count(r.*) FILTER (WHERE r.status = 'refused')                           AS refused,
       count(r.*) FILTER (WHERE r.status IN ('failed', 'errored', 'expired', 'canceled')) AS failed,
       count(DISTINCT r.document_id)                                            AS books,
       sum(cardinality(r.page_nos))                                             AS pages,
       b.est_cost_usd,
       b.actual_cost_usd,
       b.submitted_at,
       b.ended_at,
       b.collected_at,
       round(extract(epoch FROM coalesce(b.ended_at, now()) - b.submitted_at) / 3600, 2) AS hours
FROM core.llm_batch b
LEFT JOIN core.llm_batch_request r ON r.batch_id = b.batch_id
GROUP BY b.batch_id;
COMMENT ON VIEW views.books__batches IS
    'Message Batches of read_books / transcribe_books: requests by outcome, pages, estimated and actual cost (batch prices), hours to finish.';

DO $$
BEGIN
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'birddog_ui') THEN
        EXECUTE 'GRANT SELECT ON core.llm_batch, core.llm_batch_request, views.books__batches TO birddog_ui';
    END IF;
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'birddog_reader') THEN
        EXECUTE 'GRANT SELECT ON views.books__batches TO birddog_reader';
    END IF;
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'marmot_reader') THEN
        EXECUTE 'GRANT SELECT ON views.books__batches TO marmot_reader';
    END IF;
END $$;
