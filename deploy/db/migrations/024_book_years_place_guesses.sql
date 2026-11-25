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
-- (3) Confirming places (birddog/place_confirm.py, page /places/confirm in the
--     web app). Volunteers and admins confirm or correct where a book place
--     is. Every decision is a row in app.place_confirmation. A place
--     (core.place_key) is confirmed when its latest admin decision says so, or
--     when two different people's latest decisions agree; "can't tell" never
--     confirms. app.place_decide() records a decision and applies the result in
--     one statement, under a per-place advisory lock: it links the confirmed
--     location to every book the place appears in (core.document_location) and
--     removes links it made earlier for a different answer. app.place_link
--     remembers which links it made, and whether the link already existed (a
--     link it did not create is never removed).
--
-- Idempotent (re-run by every scratch test database and --all-migrations).

-- (0) shared: place keys, distances, confirmations ---------------------------------

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

CREATE TABLE IF NOT EXISTS app.place_confirmation (
    id          bigserial PRIMARY KEY,
    place_key   text NOT NULL,             -- core.place_key(normalized, admin_context)
    choice      text NOT NULL,             -- location, not_place, unsure
    location_id text,                      -- core.location, for choice = location
    user_email  text NOT NULL,
    user_role   text NOT NULL,             -- admin or user, at the time of the decision
    note        text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT place_confirmation_choice_check CHECK (choice IN ('location', 'not_place', 'unsure')),
    CONSTRAINT place_confirmation_location_check CHECK ((choice = 'location') = (location_id IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS place_confirmation_key_idx ON app.place_confirmation (place_key, created_at);
COMMENT ON TABLE app.place_confirmation IS
    'Every decision on where a book place is (page /places/confirm); the rule is views.places__confirmation_status.';

CREATE TABLE IF NOT EXISTS app.place_link (
    place_key   text NOT NULL,
    document_id bigint NOT NULL,
    location_id text NOT NULL,
    created     boolean NOT NULL,          -- false: the document_location row was already there
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (place_key, document_id, location_id)
);
COMMENT ON TABLE app.place_link IS
    'core.document_location rows made by a place confirmation (app.place_decide), so a changed answer can take them back.';

-- the rule: each person's latest decision counts; "unsure" counts for nothing;
-- an admin's latest decision wins; otherwise an answer two people agree on
CREATE OR REPLACE VIEW views.places__confirmation_status AS
WITH latest AS (
    SELECT DISTINCT ON (c.place_key, lower(c.user_email))
           c.place_key, c.user_role, c.choice, c.location_id, c.created_at
    FROM app.place_confirmation c
    ORDER BY c.place_key, lower(c.user_email), c.created_at DESC, c.id DESC
),
answer AS (
    SELECT l.place_key, l.user_role, l.created_at,
           CASE l.choice WHEN 'location' THEN 'location:' || l.location_id ELSE l.choice END AS answer
    FROM latest l
    WHERE l.choice <> 'unsure'
),
admin_pick AS (
    SELECT DISTINCT ON (a.place_key) a.place_key, a.answer
    FROM answer a WHERE a.user_role = 'admin'
    ORDER BY a.place_key, a.created_at DESC
),
votes AS (
    SELECT a.place_key, a.answer, count(*) AS n, max(a.created_at) AS last_at
    FROM answer a GROUP BY a.place_key, a.answer
),
agreed AS (
    SELECT DISTINCT ON (v.place_key) v.place_key, v.answer
    FROM votes v WHERE v.n >= 2
    ORDER BY v.place_key, v.n DESC, v.last_at DESC
),
keys AS (
    SELECT c.place_key, count(*) AS decisions, count(DISTINCT lower(c.user_email)) AS people,
           max(c.created_at) AS last_decision_at
    FROM app.place_confirmation c GROUP BY c.place_key
)
SELECT k.place_key,
       CASE WHEN coalesce(ap.answer, ag.answer) IS NOT NULL THEN 'confirmed' ELSE 'pending' END AS status,
       CASE WHEN ap.answer IS NOT NULL THEN 'admin' WHEN ag.answer IS NOT NULL THEN 'two_people' END AS confirmed_by,
       CASE WHEN coalesce(ap.answer, ag.answer) LIKE 'location:%' THEN 'location'
            ELSE coalesce(ap.answer, ag.answer) END AS answer,
       CASE WHEN coalesce(ap.answer, ag.answer) LIKE 'location:%'
            THEN substr(coalesce(ap.answer, ag.answer), 10) END AS location_id,
       k.decisions,
       k.people,
       (SELECT jsonb_object_agg(v.answer, v.n) FROM votes v WHERE v.place_key = k.place_key) AS votes,
       k.last_decision_at
FROM keys k
LEFT JOIN admin_pick ap ON ap.place_key = k.place_key
LEFT JOIN agreed ag ON ag.place_key = k.place_key;
COMMENT ON VIEW views.places__confirmation_status IS
    'Per place key: confirmed (by an admin, or two people agreeing) or pending, the answer and its location_id; votes counts each person''s latest decision.';

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
       bk.year_to   AS book_year_to,
       cs.status      AS confirmation_status,
       cs.answer      AS confirmed_answer,
       cs.location_id AS confirmed_location_id
FROM core.book_entry_place p
JOIN core.book_entry e ON e.id = p.entry_id
JOIN core.document d ON d.id = e.document_id
LEFT JOIN core.location l ON l.location_id = p.location_id
LEFT JOIN bk ON bk.document_id = e.document_id
LEFT JOIN views.places__confirmation_status cs
       ON cs.place_key = core.place_key(coalesce(nullif(p.normalized, ''), p.as_written), '');
COMMENT ON VIEW views.books__entry_places IS
    'transcribe_books: each place named in a register entry, with its matched core.location and coordinates (null when unmatched); year as in books__entries.';

-- (2) place guesses ------------------------------------------------------------

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
       (n.nearest -> 2 ->> 'km')::numeric              AS nearest_3_km,
       cs.status                                       AS confirmation_status,
       cs.answer                                       AS confirmed_answer,
       cs.location_id                                  AS confirmed_location_id,
       cl.modern_location_name                         AS confirmed_location_name
FROM place pl
JOIN core.document d ON d.id = pl.document_id
LEFT JOIN core.place_guess g ON g.key = pl.place_key
LEFT JOIN views.places__confirmation_status cs ON cs.place_key = pl.place_key
LEFT JOIN core.location cl ON cl.location_id = cs.location_id
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

-- (3) confirming places ---------------------------------------------------------

-- every place as read on a page, from both pipelines, with its key
CREATE OR REPLACE VIEW views.places__mentions AS
WITH quotes AS MATERIALIZED (
    SELECT m.document_id, m.page_no, m.as_written, min(m.quote) AS quote
    FROM views.books__place_mentions m
    WHERE coalesce(m.quote, '') <> ''
    GROUP BY m.document_id, m.page_no, m.as_written
),
read_places AS (
    SELECT b.document_id,
           coalesce(nullif(pl.value ->> 'name_uk', ''), pl.value ->> 'name_en') AS normalized,
           coalesce(pl.value ->> 'admin_context', '') AS admin_context,
           ARRAY(SELECT jsonb_array_elements_text(coalesce(pl.value -> 'variants', '[]'))) AS variants,
           pl.value -> 'pages' AS pages,
           pl.value ->> 'location_id' AS location_id,
           (pl.value ->> 'match_score')::numeric AS match_score
    FROM core.book_read b
    CROSS JOIN LATERAL jsonb_array_elements(coalesce(b.places -> 'places', '[]'::jsonb)) AS pl(value)
    WHERE b.status = 'done'
)
SELECT core.place_key(r.normalized, r.admin_context) AS place_key,
       'read_books'::text                            AS source,
       r.document_id,
       pg.page_no,
       array_to_string(r.variants, ' | ')            AS as_written,
       r.normalized,
       r.admin_context,
       r.location_id,
       r.match_score,
       (SELECT q.quote FROM quotes q
         WHERE q.document_id = r.document_id AND q.page_no = pg.page_no AND q.as_written = ANY(r.variants)
         LIMIT 1)                                    AS quote
FROM read_places r
CROSS JOIN LATERAL (SELECT x::int AS page_no FROM jsonb_array_elements_text(coalesce(r.pages, '[]')) x
                    WHERE x ~ '^[0-9]+$') pg
UNION ALL
SELECT core.place_key(coalesce(nullif(p.normalized, ''), p.as_written), ''),
       'transcribe_books',
       e.document_id,
       e.page_no,
       p.as_written,
       coalesce(nullif(p.normalized, ''), p.as_written),
       '',
       p.location_id,
       p.match_score,
       concat_ws(' · ', e.event, e.person, e.date_text)
FROM core.book_entry_place p
JOIN core.book_entry e ON e.id = p.entry_id;
COMMENT ON VIEW views.places__mentions IS
    'Every book place as read on a page (read_books and transcribe_books), keyed by core.place_key; the base of the Confirm places page.';

-- one row per place key: what to show on a card
CREATE OR REPLACE VIEW views.places__to_confirm AS
WITH m AS MATERIALIZED (
    SELECT * FROM views.places__mentions WHERE place_key <> '|'
),
auto AS (   -- the automatic match most mentions got
    SELECT DISTINCT ON (m.place_key) m.place_key, m.location_id, max(m.match_score) AS match_score
    FROM m WHERE m.location_id IS NOT NULL
    GROUP BY m.place_key, m.location_id
    ORDER BY m.place_key, count(*) DESC, m.location_id
),
agg AS (
    SELECT m.place_key,
           array_agg(DISTINCT m.source ORDER BY m.source)           AS sources,
           array_agg(DISTINCT m.document_id ORDER BY m.document_id) AS document_ids,
           count(*)                                                 AS mentions,
           string_agg(DISTINCT m.as_written, ' | ')                 AS as_written,
           min(m.normalized)                                        AS normalized,
           min(m.admin_context)                                     AS admin_context
    FROM m GROUP BY m.place_key
)
SELECT a.place_key,
       CASE WHEN au.location_id IS NULL THEN 'unmatched'
            WHEN 'read_books' = ANY(a.sources) THEN 'matched'
            ELSE 'entry' END                                       AS place_group,
       a.sources,
       a.document_ids,
       a.mentions,
       a.as_written,
       a.normalized,
       a.admin_context,
       au.location_id                                              AS auto_location_id,
       al.modern_location_name                                     AS auto_location_name,
       au.match_score,
       g.latitude                                                  AS guess_latitude,
       g.longitude                                                 AS guess_longitude,
       g.confidence                                                AS guess_confidence,
       g.basis                                                     AS guess_basis,
       (SELECT jsonb_agg(x ORDER BY x.has_quote DESC, x.document_id, x.page_no) FROM (
            SELECT DISTINCT ON (m2.document_id, m2.page_no)
                   m2.document_id, m2.page_no, d.title, m2.quote, (m2.quote IS NOT NULL) AS has_quote,
                   'https://commons.wikimedia.org/w/index.php?title='
                       || replace(substring(d.url FROM '(File:[^?#]+)$'), ' ', '_')
                       || '&page=' || m2.page_no AS page_link
            FROM m m2 JOIN core.document d ON d.id = m2.document_id
            WHERE m2.place_key = a.place_key
            ORDER BY m2.document_id, m2.page_no, (m2.quote IS NULL)
            LIMIT 6) x)                                            AS examples,
       (SELECT jsonb_agg(jsonb_build_object('location_id', n.location_id, 'name', n.name,
                                            'province', n.province, 'km', round(n.km::numeric, 1))
                         ORDER BY n.km)
          FROM (SELECT l.location_id, l.modern_location_name AS name,
                       coalesce(l.c1900_province, l.c2000_province) AS province,
                       core.km_between(coalesce(g.latitude, al.latitude), coalesce(g.longitude, al.longitude),
                                       l.latitude, l.longitude) AS km
                FROM core.location l
                WHERE l.latitude IS NOT NULL AND l.location_id IS DISTINCT FROM au.location_id
                  AND coalesce(g.latitude, al.latitude) IS NOT NULL
                ORDER BY 4 LIMIT 3) n)                             AS nearest,
       coalesce(cs.status, 'open')                                 AS confirmation_status,
       cs.answer                                                   AS confirmed_answer,
       cs.location_id                                              AS confirmed_location_id,
       cl.modern_location_name                                     AS confirmed_location_name,
       cs.confirmed_by,
       cs.votes,
       cs.decisions
FROM agg a
LEFT JOIN auto au ON au.place_key = a.place_key
LEFT JOIN core.location al ON al.location_id = au.location_id
LEFT JOIN core.place_guess g ON g.key = a.place_key
LEFT JOIN views.places__confirmation_status cs ON cs.place_key = a.place_key
LEFT JOIN core.location cl ON cl.location_id = cs.location_id;
COMMENT ON VIEW views.places__to_confirm IS
    'Confirm places page: one row per place key (place_group unmatched / matched / entry), current match or rough position, 3 nearest locations, examples with page links, confirmation state (open, pending, confirmed).';

-- record a decision and apply the rule, in one statement (autocommit callers):
-- serialized per place key, so two volunteers deciding at once cannot both link
CREATE OR REPLACE FUNCTION app.place_decide(p_key text, p_choice text, p_location_id text,
                                            p_email text, p_role text, p_note text DEFAULT NULL)
RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE
    st       record;
    docs     bigint[] := '{}';
    d        bigint;
    r        record;
    n        int;
    linked   int := 0;
    unlinked int := 0;
BEGIN
    IF p_choice NOT IN ('location', 'not_place', 'unsure') THEN
        RAISE EXCEPTION 'unknown choice %', p_choice USING ERRCODE = '22023';
    END IF;
    IF p_role NOT IN ('admin', 'user') THEN
        RAISE EXCEPTION 'unknown role %', p_role USING ERRCODE = '22023';
    END IF;
    IF p_choice = 'location' AND NOT EXISTS (SELECT 1 FROM core.location WHERE location_id = p_location_id) THEN
        RAISE EXCEPTION 'unknown location %', p_location_id USING ERRCODE = '22023';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended('place_confirm:' || p_key, 0));

    INSERT INTO app.place_confirmation (place_key, choice, location_id, user_email, user_role, note)
    VALUES (p_key, p_choice, CASE WHEN p_choice = 'location' THEN p_location_id END,
            lower(p_email), p_role, nullif(btrim(coalesce(p_note, '')), ''));

    SELECT * INTO st FROM views.places__confirmation_status WHERE place_key = p_key;
    IF st.status = 'confirmed' AND st.answer = 'location' THEN
        SELECT coalesce(array_agg(DISTINCT m.document_id), '{}') INTO docs
        FROM views.places__mentions m WHERE m.place_key = p_key;
    END IF;

    -- take back links this place made for another answer
    FOR r IN DELETE FROM app.place_link l
             WHERE l.place_key = p_key
               AND (st.location_id IS DISTINCT FROM l.location_id OR NOT l.document_id = ANY(docs))
             RETURNING l.document_id, l.location_id, l.created
    LOOP
        IF r.created THEN
            -- another confirmed place still wants this link: hand it the ownership
            UPDATE app.place_link o SET created = true
            WHERE (o.place_key, o.document_id, o.location_id) IN (
                SELECT o2.place_key, o2.document_id, o2.location_id FROM app.place_link o2
                WHERE o2.document_id = r.document_id AND o2.location_id = r.location_id LIMIT 1);
            GET DIAGNOSTICS n = ROW_COUNT;
            IF n = 0 THEN
                DELETE FROM core.document_location
                WHERE document_id = r.document_id AND location_id = r.location_id;
                unlinked := unlinked + 1;
            END IF;
        END IF;
    END LOOP;

    -- link the confirmed location to every book the place is in
    FOREACH d IN ARRAY docs LOOP
        IF NOT EXISTS (SELECT 1 FROM app.place_link l
                       WHERE l.place_key = p_key AND l.document_id = d AND l.location_id = st.location_id) THEN
            INSERT INTO core.document_location (document_id, location_id)
            VALUES (d, st.location_id) ON CONFLICT DO NOTHING;
            GET DIAGNOSTICS n = ROW_COUNT;
            INSERT INTO app.place_link (place_key, document_id, location_id, created)
            VALUES (p_key, d, st.location_id, n > 0);
            linked := linked + n;
        END IF;
    END LOOP;

    RETURN jsonb_build_object(
        'place_key', p_key,
        'status', coalesce(st.status, 'open'),
        'answer', st.answer,
        'location_id', st.location_id,
        'confirmed_by', st.confirmed_by,
        'votes', st.votes,
        'linked', linked,
        'unlinked', unlinked);
END $$;
COMMENT ON FUNCTION app.place_decide(text, text, text, text, text, text) IS
    'Record a place decision (Confirm places page) and apply views.places__confirmation_status: link or unlink core.document_location, tracked in app.place_link.';
REVOKE ALL ON FUNCTION app.place_decide(text, text, text, text, text, text) FROM PUBLIC;

-- grants -------------------------------------------------------------------------
DO $$
BEGIN
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'birddog_reader') THEN
        EXECUTE 'GRANT SELECT ON views.books__unmatched_places, views.books__entries, views.books__entry_places TO birddog_reader';
        EXECUTE 'GRANT SELECT ON views.places__to_confirm, views.places__confirmation_status, views.places__mentions TO birddog_reader';
        EXECUTE 'GRANT SELECT ON core.place_guess TO birddog_reader';
    END IF;
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'birddog_ui') THEN
        EXECUTE 'GRANT SELECT ON views.books__unmatched_places, views.places__to_confirm, views.places__confirmation_status, views.places__mentions TO birddog_ui';
        EXECUTE 'GRANT SELECT ON core.place_guess TO birddog_ui';
        EXECUTE 'GRANT DELETE ON core.place_guess TO birddog_ui';   -- delete a row to have it guessed again
    END IF;
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'marmot_reader') THEN
        EXECUTE 'GRANT SELECT ON views.books__unmatched_places TO marmot_reader';
        EXECUTE 'GRANT SELECT ON views.places__mentions, views.places__to_confirm, views.places__confirmation_status TO marmot_reader';
    END IF;
    -- the Confirm places page (birddog/place_confirm.py) runs as birddog_web
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'birddog_web') THEN
        EXECUTE 'GRANT USAGE ON SCHEMA views TO birddog_web';
        EXECUTE 'GRANT SELECT ON views.places__to_confirm, views.places__mentions, views.places__confirmation_status,
                 views.books__places, views.books__place_mentions, views.books__entry_places,
                 views.books__unmatched_places TO birddog_web';
        EXECUTE 'GRANT SELECT ON core.location, core.place_guess TO birddog_web';
        EXECUTE 'GRANT SELECT, INSERT, DELETE ON core.document_location TO birddog_web';
        EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON app.place_confirmation, app.place_link TO birddog_web';
        EXECUTE 'GRANT USAGE, SELECT ON SEQUENCE app.place_confirmation_id_seq TO birddog_web';
        EXECUTE 'GRANT EXECUTE ON FUNCTION app.place_decide(text, text, text, text, text, text) TO birddog_web';
    END IF;
END $$;
