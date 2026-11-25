-- Book years and rough positions for unmatched places.
--
-- (1) Entry years. Register entries usually give only day and month; the year
--     is on the book. core.book_year_range(document_id) reads it from the
--     title ("1848_год._Метрическая_книга...", "за_1845г", "1846-1854_годы"),
--     else from the transcript of the first 3 pages ("на 1856 годъ"). A range
--     stays a range (year_from < year_to): no single year is guessed from it.
--     views.books__entries / books__entry_places: `year` (same name, type and
--     position as in 023) is now the entry's own year, else the book's single
--     year; year_source says which ('entry', 'book' or null), and the book's
--     range is appended as book_year_from / book_year_to.
--
-- (2) Unmatched places. core.place_guess holds Claude's approximate position
--     for each place no fuzzy match linked to core.location (birddog/place_guess.py,
--     task guess_places in read_books and transcribe_books, DAG
--     guess_unmatched_places). Keyed by core.place_key(normalized, admin_context).
--     views.books__unmatched_places: one row per unmatched place per book, with
--     the guess and the 3 nearest core.location rows (haversine, computed here,
--     not stored).
--
-- Idempotent (re-run by every scratch test database and --all-migrations).

-- (1) years ---------------------------------------------------------------------

-- distinct plausible years (1700-1949) in a text; with `need_suffix`, only years
-- followed by г / год (so archive numbers like "Описи № 23 1899" are skipped)
CREATE OR REPLACE FUNCTION core.years_in(t text, need_suffix boolean DEFAULT false)
RETURNS int[] LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT coalesce(array_agg(DISTINCT m[1]::int ORDER BY m[1]::int), '{}')
    FROM regexp_matches(
             -- fond / opys / sprava numbers are not years
             regexp_replace(coalesce(t, ''), '(фонд|опись|опис|дело|справа|спр|оп|ф)\.?[ _№]*[0-9]+', ' ', 'gi'),
             CASE WHEN need_suffix
                  THEN '(?<![0-9])(1[789][0-9]{2})(?![0-9])[\s_.]*(?:г|Г|год|Год)'
                  ELSE '(?<![0-9])(1[789][0-9]{2})(?![0-9])' END,
             'g') AS m
    WHERE m[1]::int BETWEEN 1700 AND 1949
$$;

CREATE OR REPLACE FUNCTION core.book_year_range(doc_id bigint, OUT year_from int, OUT year_to int)
LANGUAGE plpgsql STABLE AS $$
DECLARE
    title_text text;
    ys int[];
BEGIN
    SELECT d.title INTO title_text FROM core.document d WHERE d.id = doc_id;
    -- a range in the title ("1846-1854_годы") stays a range
    SELECT ARRAY[m[1]::int, m[2]::int] INTO ys
    FROM regexp_matches(coalesce(title_text, ''),
                        '(?<![0-9])(1[789][0-9]{2})\s*[-–—]\s*(1[789][0-9]{2})(?![0-9])') AS m
    WHERE m[1]::int < m[2]::int
    LIMIT 1;
    IF ys IS NOT NULL THEN
        year_from := ys[1];
        year_to := ys[2];
        RETURN;
    END IF;
    -- a year marked as one ("1848_год", "за_1845г") beats a bare number
    ys := core.years_in(title_text, true);
    IF cardinality(ys) = 0 THEN
        ys := core.years_in(title_text, false);
    END IF;
    IF cardinality(ys) = 0 THEN
        SELECT core.years_in(string_agg(b.text, ' '), true) INTO ys
        FROM (SELECT DISTINCT ON (t.page_no) t.text
              FROM core.book_transcript t
              WHERE t.document_id = doc_id AND t.page_no <= 3
              ORDER BY t.page_no, (t.model = 'claude-opus-5-5') DESC, t.created_at DESC) b;
    END IF;
    IF cardinality(ys) > 0 THEN
        year_from := ys[1];
        year_to := ys[cardinality(ys)];
    END IF;
END $$;
COMMENT ON FUNCTION core.book_year_range(bigint) IS
    'Year(s) a book covers, from its title, else its first 3 transcribed pages; year_from = year_to for a single year (migration 024).';

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
       bk.year_to                                                      AS book_year_to
FROM core.book_entry e
JOIN core.document d ON d.id = e.document_id
LEFT JOIN bk ON bk.document_id = e.document_id;
COMMENT ON VIEW views.books__entries IS
    'transcribe_books: one row per register entry (sort: document_id, page_no, entry_id); year = the entry''s own, else the book''s single year (year_source). Places per entry: books__entry_places.';

CREATE OR REPLACE VIEW views.books__entry_places AS
WITH bk AS (
    SELECT d.id AS document_id, r.year_from, r.year_to
    FROM core.document d
    CROSS JOIN LATERAL core.book_year_range(d.id) r
    WHERE d.id IN (SELECT DISTINCT document_id FROM core.book_entry)
)
SELECT p.entry_id,
       p.place_idx,
       e.document_id,
       d.title,
       e.page_no,
       'https://commons.wikimedia.org/w/index.php?title='
           || replace(substring(d.url FROM '(File:[^?#]+)$'), ' ', '_')
           || '&page=' || e.page_no AS page_link,
       e.event,
       coalesce(e.year, CASE WHEN bk.year_from = bk.year_to THEN bk.year_from END) AS year,
       e.person,
       p.as_written,
       p.normalized,
       p.name_en,
       p.role,
       p.location_id,
       l.modern_location_name AS location_name,
       p.match_score,
       l.latitude,
       l.longitude,
       CASE WHEN e.year IS NOT NULL THEN 'entry'
            WHEN bk.year_from = bk.year_to THEN 'book' END AS year_source,
       bk.year_from AS book_year_from,
       bk.year_to   AS book_year_to
FROM core.book_entry_place p
JOIN core.book_entry e ON e.id = p.entry_id
JOIN core.document d ON d.id = e.document_id
LEFT JOIN core.location l ON l.location_id = p.location_id
LEFT JOIN bk ON bk.document_id = e.document_id;
COMMENT ON VIEW views.books__entry_places IS
    'transcribe_books: each place named in a register entry, with its matched core.location and coordinates (null when unmatched); year as in books__entries.';

-- (2) place guesses ------------------------------------------------------------

-- the dedup key of an unmatched place: normalized name and administrative context
CREATE OR REPLACE FUNCTION core.place_key(normalized text, admin_context text)
RETURNS text LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT lower(regexp_replace(btrim(coalesce(normalized, '')), '\s+', ' ', 'g')) || '|'
        || lower(regexp_replace(btrim(coalesce(admin_context, '')), '\s+', ' ', 'g'))
$$;

-- great-circle distance in km
CREATE OR REPLACE FUNCTION core.km_between(lat1 float8, lon1 float8, lat2 float8, lon2 float8)
RETURNS float8 LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT 2 * 6371.0 * asin(sqrt(
        power(sin(radians(lat2 - lat1) / 2), 2)
        + cos(radians(lat1)) * cos(radians(lat2)) * power(sin(radians(lon2 - lon1) / 2), 2)))
$$;

CREATE TABLE IF NOT EXISTS core.place_guess (
    key           text PRIMARY KEY,          -- core.place_key(normalized, admin_context)
    as_written    text,
    normalized    text,
    admin_context text,
    hint          text,                      -- the book title(s) given as context
    latitude      double precision,          -- null: no answer
    longitude     double precision,
    confidence    text NOT NULL,             -- high, medium, low; none = no answer
    basis         text,
    model         text,
    input_tokens  integer NOT NULL DEFAULT 0,
    output_tokens integer NOT NULL DEFAULT 0,
    cost_usd      numeric(10, 4) NOT NULL DEFAULT 0,
    run_id        text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT place_guess_confidence_check CHECK (confidence IN ('high', 'medium', 'low', 'none'))
);
COMMENT ON TABLE core.place_guess IS
    'Claude''s approximate position of a place no fuzzy match linked to core.location (birddog/place_guess.py). Delete a row to have it guessed again.';

CREATE OR REPLACE VIEW views.books__unmatched_places AS
WITH mention AS (
    -- read_books: assembled places with no match
    SELECT bp.document_id,
           'read_books'::text AS source,
           core.place_key(coalesce(nullif(bp.name_uk, ''), bp.name_en), bp.admin_context) AS place_key,
           coalesce(nullif(bp.variants, ''), bp.name_uk) AS as_written,
           coalesce(nullif(bp.name_uk, ''), bp.name_en) AS normalized,
           coalesce(bp.admin_context, '') AS admin_context,
           pg.page::int AS page_no
    FROM views.books__places bp
    CROSS JOIN LATERAL unnest(string_to_array(coalesce(bp.pages, ''), ',')) AS pg_raw(v)
    CROSS JOIN LATERAL (SELECT btrim(pg_raw.v) AS page) pg
    WHERE bp.location_id IS NULL AND pg.page ~ '^[0-9]+$'
    UNION ALL
    -- transcribe_books: places in register entries with no match
    SELECT e.document_id,
           'transcribe_books',
           core.place_key(coalesce(nullif(p.normalized, ''), p.as_written), ''),
           p.as_written,
           coalesce(nullif(p.normalized, ''), p.as_written),
           '',
           e.page_no
    FROM core.book_entry_place p
    JOIN core.book_entry e ON e.id = p.entry_id
    WHERE p.location_id IS NULL
),
place AS (
    SELECT m.document_id,
           m.place_key,
           string_agg(DISTINCT m.source, ', ')        AS sources,
           string_agg(DISTINCT m.as_written, ' | ')   AS as_written,
           min(m.normalized)                          AS normalized,
           min(m.admin_context)                       AS admin_context,
           array_agg(DISTINCT m.page_no ORDER BY m.page_no) AS pages
    FROM mention m
    WHERE m.place_key <> '|'
    GROUP BY m.document_id, m.place_key
)
SELECT pl.document_id,
       d.title,
       array_to_string(pl.pages, ', ')                 AS pages,
       'https://commons.wikimedia.org/w/index.php?title='
           || replace(substring(d.url FROM '(File:[^?#]+)$'), ' ', '_')
           || '&page=' || pl.pages[1]                  AS page_link,
       pl.sources,
       pl.as_written,
       pl.normalized,
       pl.admin_context,
       pl.place_key,
       g.latitude,
       g.longitude,
       g.confidence,
       g.basis,
       n.nearest,
       n.nearest -> 0 ->> 'location_id'                AS nearest_1_id,
       n.nearest -> 0 ->> 'name'                       AS nearest_1_name,
       (n.nearest -> 0 ->> 'km')::numeric              AS nearest_1_km,
       n.nearest -> 1 ->> 'location_id'                AS nearest_2_id,
       n.nearest -> 1 ->> 'name'                       AS nearest_2_name,
       (n.nearest -> 1 ->> 'km')::numeric              AS nearest_2_km,
       n.nearest -> 2 ->> 'location_id'                AS nearest_3_id,
       n.nearest -> 2 ->> 'name'                       AS nearest_3_name,
       (n.nearest -> 2 ->> 'km')::numeric              AS nearest_3_km
FROM place pl
JOIN core.document d ON d.id = pl.document_id
LEFT JOIN core.place_guess g ON g.key = pl.place_key
LEFT JOIN LATERAL (
    SELECT jsonb_agg(jsonb_build_object('location_id', x.location_id, 'name', x.name,
                                        'km', round(x.km::numeric, 1)) ORDER BY x.km) AS nearest
    FROM (SELECT l.location_id, l.modern_location_name AS name,
                 core.km_between(g.latitude, g.longitude, l.latitude, l.longitude) AS km
          FROM core.location l
          WHERE l.latitude IS NOT NULL AND l.longitude IS NOT NULL
          ORDER BY 3
          LIMIT 3) x
    WHERE g.latitude IS NOT NULL
) n ON true;
COMMENT ON VIEW views.books__unmatched_places IS
    'Places in book readings with no core.location match: one row per place per book, Claude''s rough position (latitude/longitude, confidence, basis) and the 3 nearest known locations.';

-- grants -------------------------------------------------------------------------
DO $$
BEGIN
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'birddog_reader') THEN
        EXECUTE 'GRANT SELECT ON views.books__unmatched_places, views.books__entries, views.books__entry_places TO birddog_reader';
        EXECUTE 'GRANT SELECT ON core.place_guess TO birddog_reader';
    END IF;
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'birddog_ui') THEN
        EXECUTE 'GRANT SELECT ON views.books__unmatched_places TO birddog_ui';
        EXECUTE 'GRANT SELECT ON core.place_guess TO birddog_ui';
        EXECUTE 'GRANT DELETE ON core.place_guess TO birddog_ui';   -- delete a row to have it guessed again
    END IF;
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'marmot_reader') THEN
        EXECUTE 'GRANT SELECT ON views.books__unmatched_places TO marmot_reader';
    END IF;
END $$;
