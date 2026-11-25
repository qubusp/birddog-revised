-- Fix 019: views.books__places cast location_id to bigint, but
-- core.location.location_id is text (e.g. 'A0042', '-1034035'), so any query
-- touching the column failed. CREATE OR REPLACE VIEW cannot change a column's
-- type, so a view still on the old type is dropped and recreated.
-- Idempotent.

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
       p.value ->> 'location_id'               AS location_id,
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

DO $$
BEGIN
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'birddog_ui') THEN
        EXECUTE 'GRANT SELECT ON views.books__places TO birddog_ui';
    END IF;
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'marmot_reader') THEN
        EXECUTE 'GRANT SELECT ON views.books__places TO marmot_reader';
    END IF;
END $$;
