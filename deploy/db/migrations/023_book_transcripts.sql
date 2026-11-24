-- Book transcription: Claude transcribes every page of a book in full (text,
-- register entries, places), on top of the place reading of 017. DAG:
-- transcribe_books in dags/yaml/books.yml; code: birddog/book_transcripts.py.
--
--   core.book_transcript_queue  one row per book to transcribe; its own status,
--                               so the read_books rows (core.book_read,
--                               core.book_page) stay as the place pilot left them
--   core.book_transcript        one row per page and model: text, entries, places
--   core.book_entry             one row per register entry, from each page's best
--                               reading (Opus re-read over Sonnet), for search
--   core.book_entry_place       the places named in an entry, matched to
--                               core.location like views.books__places
--
-- Search: core.search_books(q). Full text uses the 'simple' config (the text
-- mixes Cyrillic with pre-1918 letters, Latin, Hebrew and Yiddish, which no
-- stemmer handles), with a pg_trgm fallback for misspellings.
--
-- Idempotent (re-run by every scratch test database and --all-migrations).

CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS core.book_transcript_queue (
    document_id   bigint PRIMARY KEY REFERENCES core.document(id) ON DELETE CASCADE,
    status        text NOT NULL DEFAULT 'pending',
    page_count    integer,
    input_tokens  bigint NOT NULL DEFAULT 0,
    output_tokens bigint NOT NULL DEFAULT 0,
    cost_usd      numeric(10, 4) NOT NULL DEFAULT 0,
    error         text,
    run_id        text,             -- Airflow run that last worked on it
    queued_at     timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT book_transcript_queue_status_check CHECK (status IN (
        'pending', 'transcribing', 'transcribed', 'done', 'failed'))
);
CREATE INDEX IF NOT EXISTS book_transcript_queue_status_idx ON core.book_transcript_queue (status);
COMMENT ON TABLE core.book_transcript_queue IS
    'transcribe_books queue: pending -> transcribing -> transcribed -> done (entries indexed); failed: see error, set back to pending to retry.';

DROP TRIGGER IF EXISTS book_transcript_queue_touch ON core.book_transcript_queue;
CREATE TRIGGER book_transcript_queue_touch BEFORE UPDATE ON core.book_transcript_queue
    FOR EACH ROW EXECUTE FUNCTION core.book_read_touch();

CREATE TABLE IF NOT EXISTS core.book_transcript (
    document_id   bigint NOT NULL REFERENCES core.document(id) ON DELETE CASCADE,
    page_no       integer NOT NULL,
    model         text NOT NULL,
    illegible     boolean NOT NULL DEFAULT false,
    text          text NOT NULL DEFAULT '',
    entries       jsonb NOT NULL DEFAULT '[]',
    places        jsonb NOT NULL DEFAULT '[]',
    notes         text,
    input_tokens  integer NOT NULL DEFAULT 0,
    output_tokens integer NOT NULL DEFAULT 0,
    cost_usd      numeric(10, 4) NOT NULL DEFAULT 0,
    created_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (document_id, page_no, model)
);
COMMENT ON TABLE core.book_transcript IS
    'Full transcription of a book page by one model; a page re-read by Opus gets a second row, which wins.';
CREATE INDEX IF NOT EXISTS book_transcript_fts_idx
    ON core.book_transcript USING gin (to_tsvector('simple', text));
CREATE INDEX IF NOT EXISTS book_transcript_trgm_idx
    ON core.book_transcript USING gin (text gin_trgm_ops);

CREATE TABLE IF NOT EXISTS core.book_entry (
    id          bigserial PRIMARY KEY,
    document_id bigint NOT NULL REFERENCES core.document(id) ON DELETE CASCADE,
    page_no     integer NOT NULL,
    entry_idx   integer NOT NULL,     -- order on the page, from 1
    model       text NOT NULL,        -- the reading it came from
    entry_no    text,                 -- as written
    event       text,                 -- birth, marriage, death, divorce, other
    date_text   text,                 -- as written
    date_iso    text,                 -- YYYY, YYYY-MM or YYYY-MM-DD, only when certain
    year        integer,              -- from date_iso, or from date_text when it names one year
    person      text,
    father      text,
    mother      text,
    others      jsonb NOT NULL DEFAULT '[]',   -- [{name, role}]
    places      jsonb NOT NULL DEFAULT '[]',   -- [{as_written, normalized, name_en, role}]
    note        text,
    UNIQUE (document_id, page_no, entry_idx)
);
COMMENT ON TABLE core.book_entry IS
    'Register entries from book transcripts (best reading per page), rebuilt per book by transcribe_books.';
CREATE INDEX IF NOT EXISTS book_entry_doc_idx ON core.book_entry (document_id, page_no);
CREATE INDEX IF NOT EXISTS book_entry_fts_idx ON core.book_entry USING gin (
    to_tsvector('simple', coalesce(person, '') || ' ' || coalesce(father, '') || ' '
                          || coalesce(mother, '') || ' ' || coalesce(note, '')));
CREATE INDEX IF NOT EXISTS book_entry_person_trgm_idx ON core.book_entry USING gin (person gin_trgm_ops);

CREATE TABLE IF NOT EXISTS core.book_entry_place (
    entry_id    bigint NOT NULL REFERENCES core.book_entry(id) ON DELETE CASCADE,
    place_idx   integer NOT NULL,
    as_written  text,
    normalized  text,
    name_en     text,
    role        text,
    location_id text,                 -- core.location, matched as books.match_places
    match_score numeric,
    PRIMARY KEY (entry_id, place_idx)
);
COMMENT ON TABLE core.book_entry_place IS
    'Places named in a register entry, fuzzy-matched to core.location (location_id null when no match).';
CREATE INDEX IF NOT EXISTS book_entry_place_location_idx ON core.book_entry_place (location_id);

-- views: Mathesar (read-only), Grafana --------------------------------------
-- page_link opens that page of the scan on Commons, as in 019.

CREATE OR REPLACE VIEW views.books__transcripts AS
WITH best AS (
    SELECT DISTINCT ON (t.document_id, t.page_no) t.*
    FROM core.book_transcript t
    ORDER BY t.document_id, t.page_no, (t.model = 'claude-opus-5-5') DESC, t.created_at DESC
)
SELECT best.document_id,
       d.title,
       best.page_no,
       'https://commons.wikimedia.org/w/index.php?title='
           || replace(substring(d.url FROM '(File:[^?#]+)$'), ' ', '_')
           || '&page=' || best.page_no AS page_link,
       best.model,
       best.illegible,
       jsonb_array_length(best.entries)  AS entries,
       jsonb_array_length(best.places)   AS places,
       length(best.text)                 AS text_chars,
       best.text,
       best.notes,
       best.cost_usd,
       best.created_at
FROM best
JOIN core.document d ON d.id = best.document_id;
COMMENT ON VIEW views.books__transcripts IS
    'transcribe_books: the best transcription of each page, with a link to the scan (sort: document_id, page_no).';

CREATE OR REPLACE VIEW views.books__entries AS
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
       e.year,
       e.person,
       e.father,
       e.mother,
       (SELECT string_agg(coalesce(o ->> 'name', '') || ' (' || coalesce(o ->> 'role', '') || ')', '; ')
          FROM jsonb_array_elements(e.others) o)                       AS others,
       (SELECT string_agg(coalesce(p.as_written, '') || coalesce(' [' || nullif(p.role, '') || ']', ''), '; '
                          ORDER BY p.place_idx)
          FROM core.book_entry_place p WHERE p.entry_id = e.id)        AS places,
       e.note,
       e.model
FROM core.book_entry e
JOIN core.document d ON d.id = e.document_id;
COMMENT ON VIEW views.books__entries IS
    'transcribe_books: one row per register entry (sort: document_id, page_no, entry_id). Places per entry: books__entry_places.';

CREATE OR REPLACE VIEW views.books__entry_places AS
SELECT p.entry_id,
       p.place_idx,
       e.document_id,
       d.title,
       e.page_no,
       'https://commons.wikimedia.org/w/index.php?title='
           || replace(substring(d.url FROM '(File:[^?#]+)$'), ' ', '_')
           || '&page=' || e.page_no AS page_link,
       e.event,
       e.year,
       e.person,
       p.as_written,
       p.normalized,
       p.name_en,
       p.role,
       p.location_id,
       l.modern_location_name AS location_name,
       p.match_score,
       l.latitude,
       l.longitude
FROM core.book_entry_place p
JOIN core.book_entry e ON e.id = p.entry_id
JOIN core.document d ON d.id = e.document_id
LEFT JOIN core.location l ON l.location_id = p.location_id;
COMMENT ON VIEW views.books__entry_places IS
    'transcribe_books: each place named in a register entry, with its matched core.location and coordinates (null when unmatched).';

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
       q.updated_at
FROM core.book_transcript_queue q
JOIN core.document d ON d.id = q.document_id;
COMMENT ON VIEW views.books__transcription_progress IS
    'transcribe_books: per book status, pages done, entries found and spend.';

-- search ---------------------------------------------------------------------
-- Keyword hits over the best transcript of each page first; when there are
-- fewer than max_rows, pages whose text contains a close spelling of the query
-- (pg_trgm word similarity) fill up.
SELECT word_similarity('', '');   -- loads pg_trgm so the SET below is allowed
CREATE OR REPLACE FUNCTION core.search_books(q text, max_rows int DEFAULT 50)
RETURNS TABLE (document_id bigint, page_no int, snippet text, page_link text, rank real)
LANGUAGE plpgsql STABLE
SET pg_trgm.word_similarity_threshold = 0.6
AS $$
#variable_conflict use_column
DECLARE
    words tsquery := websearch_to_tsquery('simple', q);
    found int;
BEGIN
    RETURN QUERY
    WITH best AS (
        SELECT DISTINCT ON (t.document_id, t.page_no) t.document_id, t.page_no, t.text
        FROM core.book_transcript t
        WHERE to_tsvector('simple', t.text) @@ words
        ORDER BY t.document_id, t.page_no, (t.model = 'claude-opus-5-5') DESC, t.created_at DESC
    )
    SELECT b.document_id, b.page_no,
           ts_headline('simple', b.text, words, 'MaxFragments=2, MaxWords=20, MinWords=5'),
           'https://commons.wikimedia.org/w/index.php?title='
               || replace(substring(d.url FROM '(File:[^?#]+)$'), ' ', '_') || '&page=' || b.page_no,
           ts_rank(to_tsvector('simple', b.text), words)::real
    FROM best b JOIN core.document d ON d.id = b.document_id
    ORDER BY 5 DESC
    LIMIT max_rows;
    GET DIAGNOSTICS found = ROW_COUNT;

    IF found < max_rows THEN
        RETURN QUERY
        WITH best AS (
            SELECT DISTINCT ON (t.document_id, t.page_no) t.document_id, t.page_no, t.text
            FROM core.book_transcript t
            WHERE q <% t.text AND NOT to_tsvector('simple', t.text) @@ words
            ORDER BY t.document_id, t.page_no, (t.model = 'claude-opus-5-5') DESC, t.created_at DESC
        )
        SELECT b.document_id, b.page_no,
               left(b.text, 200),
               'https://commons.wikimedia.org/w/index.php?title='
                   || replace(substring(d.url FROM '(File:[^?#]+)$'), ' ', '_') || '&page=' || b.page_no,
               word_similarity(q, b.text)::real
        FROM best b JOIN core.document d ON d.id = b.document_id
        ORDER BY 5 DESC
        LIMIT max_rows - found;
    END IF;
END
$$;
COMMENT ON FUNCTION core.search_books(text, int) IS
    'Search book transcripts: full text (simple config), then trigram word similarity. Returns page links to the scans.';

-- grants ---------------------------------------------------------------------
DO $$
DECLARE
    v text := 'views.books__transcripts, views.books__entries, views.books__entry_places, views.books__transcription_progress';
BEGIN
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'birddog_ui') THEN
        EXECUTE 'GRANT SELECT ON core.book_transcript_queue, core.book_transcript, core.book_entry, core.book_entry_place TO birddog_ui';
        EXECUTE 'GRANT UPDATE (status) ON core.book_transcript_queue TO birddog_ui';  -- retry a failed book
        EXECUTE 'GRANT SELECT ON ' || v || ' TO birddog_ui';
        EXECUTE 'GRANT EXECUTE ON FUNCTION core.search_books(text, int) TO birddog_ui';
    END IF;
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'marmot_reader') THEN
        EXECUTE 'GRANT SELECT ON ' || v || ' TO marmot_reader';
    END IF;
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'birddog_reader') THEN
        EXECUTE 'GRANT USAGE ON SCHEMA views TO birddog_reader';
        EXECUTE 'GRANT SELECT ON ' || v || ' TO birddog_reader';
        EXECUTE 'GRANT EXECUTE ON FUNCTION core.search_books(text, int) TO birddog_reader';
    END IF;
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'birddog_web') THEN
        EXECUTE 'GRANT EXECUTE ON FUNCTION core.search_books(text, int) TO birddog_web';
    END IF;
END $$;
