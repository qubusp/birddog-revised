-- Keyword + fuzzy search over pages and documents.
--   keyword: full-text on the 'simple' config (no Ukrainian stemmer ships with
--            Postgres; 'simple' lowercases and splits, which suits archive
--            codes and names). / - _ : . count as spaces, so "ДАЗО 1-1-12",
--            "ДАЗО/1/1/12" and "File:ДАЗО_1-1-12.pdf" share the same words.
--   fuzzy:   pg_trgm on titles/labels, for typos ("ЦДIАК" with a Latin I);
--            only used to fill up when the keyword search finds too little
-- Expression indexes, so no table rewrite and nothing for writers to maintain.

CREATE EXTENSION IF NOT EXISTS pg_trgm;   -- trusted extension: the db owner may create it

CREATE OR REPLACE FUNCTION core.search_vector(VARIADIC parts text[]) RETURNS tsvector
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT to_tsvector('simple', translate(array_to_string(parts, ' '), '/-_:.', '     '))
$$;

-- first version of this file indexed the raw text
DROP INDEX IF EXISTS core.page_fts_idx;
DROP INDEX IF EXISTS core.document_fts_idx;

CREATE INDEX IF NOT EXISTS page_search_idx ON core.page
    USING gin (core.search_vector(title, native_description, description_en));
CREATE INDEX IF NOT EXISTS page_title_trgm_idx ON core.page USING gin (title gin_trgm_ops);
CREATE INDEX IF NOT EXISTS page_label_trgm_idx ON core.page USING gin (label gin_trgm_ops);

CREATE INDEX IF NOT EXISTS document_search_idx ON core.document
    USING gin (core.search_vector(title, description));
CREATE INDEX IF NOT EXISTS document_title_trgm_idx ON core.document USING gin (title gin_trgm_ops);

-- One entry point for the UI, notebooks and the apps:
--   SELECT * FROM core.search('ДАЗО 1-1-12');
-- Keyword hits come first (the page whose code is the query, then exact
-- phrase, then all words), then fuzzy
-- title matches fill up to max_rows.
DROP FUNCTION IF EXISTS core.search(text, int);
-- loads pg_trgm so its setting exists for the SET clause below (an unloaded,
-- placeholder setting may only be set by a superuser)
SELECT similarity('', '');
CREATE FUNCTION core.search(q text, max_rows int DEFAULT 50)
RETURNS TABLE (kind text, id bigint, title text, url text, snippet text, score real)
LANGUAGE plpgsql STABLE
-- 0.3 (the default) lets short codes match most of the table
SET pg_trgm.similarity_threshold = 0.5
AS $$
#variable_conflict use_column
DECLARE
    words  tsquery := websearch_to_tsquery('simple', translate(q, '/-_:.', '     '));
    phrase tsquery := phraseto_tsquery('simple', translate(q, '/-_:.', '     '));
    -- the query as a bare code ("дахмо 315"), to rank the page it names first
    code   text := lower(trim(regexp_replace(q, '[/_:.\s-]+', ' ', 'g')));
    found  int;
BEGIN
    RETURN QUERY
    SELECT h.* FROM (
        SELECT 'page'::text, p.id, p.title, p.url,
               coalesce(p.description_en, p.native_description),
               (ts_rank(v, words) + (v @@ phrase)::int
                + 2 * (lower(regexp_replace(p.title, '[/_:.\s-]+', ' ', 'g')) = code)::int)::real AS score
        FROM core.page p,
             LATERAL (SELECT core.search_vector(p.title, p.native_description, p.description_en) AS v) s
        WHERE core.search_vector(p.title, p.native_description, p.description_en) @@ words
        UNION ALL
        SELECT 'document', d.id, d.title, d.url, d.description,
               (ts_rank(v, words) + (v @@ phrase)::int)::real
        FROM core.document d,
             LATERAL (SELECT core.search_vector(d.title, d.description) AS v) s
        WHERE core.search_vector(d.title, d.description) @@ words
    ) h
    ORDER BY h.score DESC
    LIMIT max_rows;
    GET DIAGNOSTICS found = ROW_COUNT;

    IF found < max_rows THEN
        RETURN QUERY
        SELECT h.* FROM (
            SELECT 'page'::text, p.id, p.title, p.url,
                   coalesce(p.description_en, p.native_description),
                   greatest(similarity(p.title, q), similarity(coalesce(p.label, ''), q))::real AS score
            FROM core.page p
            WHERE (p.title % q OR p.label % q)
              AND NOT core.search_vector(p.title, p.native_description, p.description_en) @@ words
            UNION ALL
            SELECT 'document', d.id, d.title, d.url, d.description, similarity(d.title, q)::real
            FROM core.document d
            WHERE d.title % q
              AND NOT core.search_vector(d.title, d.description) @@ words
        ) h
        ORDER BY h.score DESC
        LIMIT max_rows - found;
    END IF;
END
$$;

GRANT EXECUTE ON FUNCTION core.search(text, int) TO birddog_ui, birddog_reader;
