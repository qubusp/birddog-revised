-- Keyword + fuzzy search over pages and documents.
--   keyword: full-text on the 'simple' config (no Ukrainian stemmer ships with
--            Postgres; 'simple' lowercases and splits, which suits archive
--            codes and names)
--   fuzzy:   pg_trgm on titles/labels, for typos and partial codes (ILIKE '%..%')
-- Expression indexes, so no table rewrite and nothing for writers to maintain.

CREATE EXTENSION IF NOT EXISTS pg_trgm;   -- trusted extension: the db owner may create it

CREATE INDEX IF NOT EXISTS page_fts_idx ON core.page USING gin (
    to_tsvector('simple', coalesce(title, '') || ' ' || coalesce(native_description, '')
                          || ' ' || coalesce(description_en, '')));
CREATE INDEX IF NOT EXISTS page_title_trgm_idx ON core.page USING gin (title gin_trgm_ops);
CREATE INDEX IF NOT EXISTS page_label_trgm_idx ON core.page USING gin (label gin_trgm_ops);

CREATE INDEX IF NOT EXISTS document_fts_idx ON core.document USING gin (
    to_tsvector('simple', coalesce(title, '') || ' ' || coalesce(description, '')));
CREATE INDEX IF NOT EXISTS document_title_trgm_idx ON core.document USING gin (title gin_trgm_ops);

-- One entry point for the UI, notebooks and the apps:
--   SELECT * FROM core.search('ДАЗО 1 опис 2');
-- Rank: full-text hits first, then by trigram similarity of the title/label.
CREATE OR REPLACE FUNCTION core.search(q text, max_rows int DEFAULT 50)
RETURNS TABLE (kind text, id bigint, title text, url text, snippet text, score real)
LANGUAGE sql STABLE AS $$
    WITH query AS (SELECT websearch_to_tsquery('simple', q) AS tsq)
    (SELECT 'page' AS kind, p.id, p.title, p.url,
            coalesce(p.description_en, p.native_description) AS snippet,
            (ts_rank(to_tsvector('simple', coalesce(p.title, '') || ' ' || coalesce(p.native_description, '')
                                           || ' ' || coalesce(p.description_en, '')), query.tsq)
             + greatest(similarity(p.title, q), similarity(coalesce(p.label, ''), q)))::real AS score
     FROM core.page p, query
     WHERE to_tsvector('simple', coalesce(p.title, '') || ' ' || coalesce(p.native_description, '')
                                 || ' ' || coalesce(p.description_en, '')) @@ query.tsq
        OR p.title % q OR p.label % q OR p.title ILIKE '%' || q || '%')
    UNION ALL
    (SELECT 'document', d.id, d.title, d.url, d.description,
            (ts_rank(to_tsvector('simple', coalesce(d.title, '') || ' ' || coalesce(d.description, '')), query.tsq)
             + similarity(coalesce(d.title, ''), q))::real
     FROM core.document d, query
     WHERE to_tsvector('simple', coalesce(d.title, '') || ' ' || coalesce(d.description, '')) @@ query.tsq
        OR d.title % q OR d.title ILIKE '%' || q || '%')
    ORDER BY score DESC
    LIMIT max_rows
$$;

GRANT EXECUTE ON FUNCTION core.search(text, int) TO birddog_ui, birddog_reader;
