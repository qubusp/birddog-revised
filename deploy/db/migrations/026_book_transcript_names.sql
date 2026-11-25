-- Names in book transcriptions (birddog/book_transcripts.py, DAG transcribe_books).
--
-- Sonnet left 332 of the pilot's 2,315 register entries (14%) without a
-- readable name; Opus, asked for its best reading with each uncertain letter
-- marked "?", read them. So Opus now re-reads a page when it is illegible or
-- when its names came back unreadable, with that "names" prompt, and a re-run
-- mode redoes such pages in books already transcribed.
--
--   core.book_transcript.prompt         'names' when read with the names prompt
--   core.book_transcript_queue.status   + 'reread_names' (queued for the re-run)
--   core.name_unreadable(person)        empty, or nothing but [...] / ?
--   core.names_uncertain(p, f, m)       how many of person/father/mother were
--                                       read with a ? or a [...] gap
--   views.books__entries                + name_unreadable, names_uncertain
--   views.books__transcription_progress + entries_name_unreadable,
--                                         entries_names_uncertain, pages_names_reread
-- (columns appended only: both views are deployed and Grafana reads them).
--
-- Idempotent (re-run by every scratch test database and --all-migrations).

ALTER TABLE core.book_transcript ADD COLUMN IF NOT EXISTS prompt text;
COMMENT ON COLUMN core.book_transcript.prompt IS
    'Prompt variant the page was read with: null = the standard one, names = best reading of every name, uncertain letters marked ?';

ALTER TABLE core.book_transcript_queue DROP CONSTRAINT IF EXISTS book_transcript_queue_status_check;
ALTER TABLE core.book_transcript_queue ADD CONSTRAINT book_transcript_queue_status_check CHECK (status IN (
    'pending', 'transcribing', 'transcribed', 'done', 'failed', 'reread_names'));
COMMENT ON TABLE core.book_transcript_queue IS
    'transcribe_books queue: pending -> transcribing -> transcribed -> done (entries indexed); reread_names: Opus re-reads pages with unreadable names, then transcribed; failed: see error, set back to pending to retry.';

-- same rule as book_transcripts.unreadable_name()
CREATE OR REPLACE FUNCTION core.name_unreadable(person text)
RETURNS boolean LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT coalesce(person, '') ~ '^\s*((\[(\.\.\.|…)\]|\?+)\s*)*$'
$$;

CREATE OR REPLACE FUNCTION core.names_uncertain(person text, father text, mother text)
RETURNS integer LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT count(*)::int FROM unnest(ARRAY[person, father, mother]) AS n(v)
    WHERE coalesce(n.v, '') <> '' AND NOT core.name_unreadable(n.v)
      AND n.v ~ '\?|\[(\.\.\.|…)\]'
$$;

CREATE OR REPLACE VIEW views.books__entries AS
WITH bk AS (
    SELECT d.id AS document_id, r.year_from, r.year_to
    FROM core.document d
    CROSS JOIN LATERAL core.book_year_range(d.id) r
    WHERE d.id IN (SELECT DISTINCT document_id FROM core.book_entry)
)
SELECT e.id AS entry_id,
       e.document_id,
       d.title,
       e.page_no,
       'https://commons.wikimedia.org/w/index.php?title='
           || replace(substring(d.url FROM '(File:[^?#]+)$'), ' ', '_')
           || '&page=' || e.page_no AS page_link,
       e.entry_no,
       e.event,
       e.date_text,
       e.date_iso,
       coalesce(e.year, CASE WHEN bk.year_from = bk.year_to THEN bk.year_from END) AS year,
       e.person,
       e.father,
       e.mother,
       (SELECT string_agg(coalesce(o ->> 'name', '') || ' (' || coalesce(o ->> 'role', '') || ')', '; ')
          FROM jsonb_array_elements(e.others) o)                       AS others,
       (SELECT string_agg(coalesce(p.as_written, '') || coalesce(' [' || nullif(p.role, '') || ']', ''), '; '
                          ORDER BY p.place_idx)
          FROM core.book_entry_place p WHERE p.entry_id = e.id)        AS places,
       e.note,
       e.model,
       CASE WHEN e.year IS NOT NULL THEN 'entry'
            WHEN bk.year_from = bk.year_to THEN 'book' END             AS year_source,
       bk.year_from                                                    AS book_year_from,
       bk.year_to                                                      AS book_year_to,
       core.name_unreadable(e.person)                                  AS name_unreadable,
       core.names_uncertain(e.person, e.father, e.mother)              AS names_uncertain
FROM core.book_entry e
JOIN core.document d ON d.id = e.document_id
LEFT JOIN bk ON bk.document_id = e.document_id;
COMMENT ON VIEW views.books__entries IS
    'transcribe_books: one row per register entry (sort: document_id, page_no, entry_id); year = the entry''s own, else the book''s single year (year_source); name_unreadable / names_uncertain say how sure the names are. Places per entry: books__entry_places.';

CREATE OR REPLACE VIEW views.books__transcription_progress AS
SELECT q.document_id,
       d.title,
       q.status,
       q.page_count,
       (SELECT count(DISTINCT t.page_no) FROM core.book_transcript t
         WHERE t.document_id = q.document_id)                         AS pages_transcribed,
       (SELECT count(*) FROM core.book_transcript t
         WHERE t.document_id = q.document_id AND t.model = 'claude-opus-5-5') AS pages_reread,
       (SELECT count(*) FROM core.book_entry e WHERE e.document_id = q.document_id) AS entries,
       q.input_tokens,
       q.output_tokens,
       q.cost_usd,
       round(q.cost_usd / nullif((SELECT count(DISTINCT t.page_no) FROM core.book_transcript t
                                   WHERE t.document_id = q.document_id), 0), 4) AS cost_per_page,
       q.error,
       q.run_id,
       q.updated_at,
       (SELECT count(*) FROM core.book_entry e
         WHERE e.document_id = q.document_id AND core.name_unreadable(e.person))      AS entries_name_unreadable,
       (SELECT count(*) FROM core.book_entry e
         WHERE e.document_id = q.document_id
           AND core.names_uncertain(e.person, e.father, e.mother) > 0)               AS entries_names_uncertain,
       (SELECT count(*) FROM core.book_transcript t
         WHERE t.document_id = q.document_id AND t.prompt = 'names')                  AS pages_names_reread
FROM core.book_transcript_queue q
JOIN core.document d ON d.id = q.document_id;
COMMENT ON VIEW views.books__transcription_progress IS
    'transcribe_books: per book status, pages done, entries found, how many names are unreadable or uncertain, and spend.';

DO $$
BEGIN
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'birddog_reader') THEN
        EXECUTE 'GRANT SELECT ON views.books__entries, views.books__transcription_progress TO birddog_reader';
    END IF;
END $$;
