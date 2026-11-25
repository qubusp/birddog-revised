-- Review views for the read_books pilot (migration 017, birddog/books.py), for
-- checking Claude's readings against the scans in Mathesar.
--
--   views.books__places          one row per assembled place per book: names,
--                                mention count, pages, matched core.location
--   views.books__place_mentions  one row per place as read on a page, with the
--                                transcribed quote, the model that read it and a
--                                link that opens that page of the scan on Commons
--
-- Mentions come from each page's best reading: the Opus re-read when there is
-- one, else Sonnet's. Read-only (Mathesar shows views read-only). Idempotent.

-- Drop only while the view still has the old bigint location_id (pre-020): views
-- in 021 (map__locations, map__book_places) depend on this one, so an
-- unconditional DROP would fail on re-runs (or, with CASCADE, delete them).
DO $$
BEGIN
    IF (SELECT data_type FROM information_schema.columns
        WHERE table_schema = 'views' AND table_name = 'books__places'
          AND column_name = 'location_id') = 'bigint' THEN
        DROP VIEW views.books__places CASCADE;
    END IF;
END $$;
CREATE OR REPLACE VIEW views.books__places AS
SELECT b.document_id,
       d.title,
       p.ordinality::int                       AS place_no,
       p.value ->> 'name_uk'                   AS name_uk,
       p.value ->> 'name_en'                   AS name_en,
       (p.value ->> 'mentions')::int           AS mentions,
       p.value ->> 'admin_context'             AS admin_context,
       (SELECT string_agg(x, ', ') FROM jsonb_array_elements_text(p.value -> 'pages') x) AS pages,
       (SELECT string_agg(x, ' | ') FROM jsonb_array_elements_text(p.value -> 'variants') x) AS variants,
       (SELECT string_agg(x, ', ') FROM jsonb_array_elements_text(p.value -> 'kinds') x) AS kinds,
       p.value ->> 'location_id'               AS location_id,   -- text, as core.location
       p.value ->> 'location_name'             AS location_name,
       (p.value ->> 'match_score')::numeric    AS match_score,
       d.url
FROM core.book_read b
JOIN core.document d ON d.id = b.document_id
CROSS JOIN LATERAL jsonb_array_elements(coalesce(b.places -> 'places', '[]'::jsonb))
     WITH ORDINALITY AS p(value, ordinality)
WHERE b.status = 'done';
COMMENT ON VIEW views.books__places IS
    'read_books results: one row per place per book (sort: document_id, mentions desc). Quotes per page: books__place_mentions.';

CREATE OR REPLACE VIEW views.books__place_mentions AS
WITH best AS (
    SELECT DISTINCT ON (bp.document_id, bp.page_no)
           bp.document_id, bp.page_no, bp.model, bp.result
    FROM core.book_page bp
    ORDER BY bp.document_id, bp.page_no,
             (bp.model = 'claude-opus-5-5') DESC, bp.created_at DESC
)
SELECT best.document_id,
       best.page_no,
       m.value ->> 'as_written'    AS as_written,
       m.value ->> 'normalized'    AS normalized,
       m.value ->> 'kind'          AS kind,
       m.value ->> 'admin_context' AS admin_context,
       m.value ->> 'quote'         AS quote,
       m.value ->> 'confidence'    AS confidence,
       best.model,
       'https://commons.wikimedia.org/w/index.php?title='
           || replace(substring(d.url FROM '(File:[^?#]+)$'), ' ', '_')
           || '&page=' || best.page_no AS page_link,
       d.title
FROM best
JOIN core.document d ON d.id = best.document_id
CROSS JOIN LATERAL jsonb_array_elements(coalesce(best.result -> 'places', '[]'::jsonb)) AS m(value);
COMMENT ON VIEW views.books__place_mentions IS
    'read_books: every place as read on a page, with quote and a link to that page of the scan (sort: document_id, page_no).';

DO $$
BEGIN
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'birddog_ui') THEN
        EXECUTE 'GRANT SELECT ON views.books__places, views.books__place_mentions TO birddog_ui';
    END IF;
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'marmot_reader') THEN
        EXECUTE 'GRANT SELECT ON views.books__places, views.books__place_mentions TO marmot_reader';
    END IF;
END $$;
