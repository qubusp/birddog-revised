-- Objects for the Saltcorn "Curator" app (deploy/apps/saltcorn). Saltcorn
-- reaches Birddog through the postgres-tables plugin as birddog_ui, and that
-- plugin can only read and write tables and views: it builds
-- `SELECT ... FROM schema.table WHERE col = $1`, so it cannot pass an
-- argument to core.search(). Instead the search form inserts the query text
-- into saltcorn.search_request, and saltcorn.search_hit runs core.search()
-- for each distinct query; the results list filters it with q = <query>,
-- which Postgres pushes into the DISTINCT subquery, so only that one search
-- runs. search_request doubles as a log of curator searches.
--
-- Everything lives in its own schema `saltcorn`, so it can be dropped as a
-- unit. Additive and idempotent; the migrate service re-runs it on every
-- deploy. CREATE OR REPLACE VIEW can only append columns: to drop, rename
-- or reorder a view's columns, DROP VIEW it first in this file.

CREATE SCHEMA IF NOT EXISTS saltcorn;
COMMENT ON SCHEMA saltcorn IS 'Helpers for the Saltcorn curator app (deploy/apps/saltcorn)';

CREATE TABLE IF NOT EXISTS saltcorn.search_request (
    id           bigserial PRIMARY KEY,
    q            text NOT NULL CHECK (length(btrim(q)) BETWEEN 1 AND 200),
    requested_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS search_request_q ON saltcorn.search_request (q);
COMMENT ON TABLE saltcorn.search_request IS
    'Search texts submitted in the Saltcorn curator app; saltcorn.search_hit runs core.search() on them.';

-- one row per (query, hit); hit_id is the key Saltcorn needs (kind:id)
CREATE OR REPLACE VIEW saltcorn.search_hit AS
SELECT r.q,
       s.kind || ':' || s.id AS hit_id,
       s.kind, s.id, s.title, s.url,
       left(s.snippet, 300)  AS snippet,
       s.score
FROM (SELECT DISTINCT q FROM saltcorn.search_request) r
CROSS JOIN LATERAL core.search(r.q, 50) s;
COMMENT ON VIEW saltcorn.search_hit IS
    'core.search(q, 50) for every q in saltcorn.search_request. Always filter on q.';

-- page <-> document links with display fields from both sides, so the page
-- detail can list its documents and the document detail its owning pages
CREATE OR REPLACE VIEW saltcorn.page_document_link AS
SELECT pd.page_id || ':' || pd.document_id AS link_id,
       pd.page_id, p.title AS page_title, p.url AS page_url,
       pd.document_id, d.title AS document_title, d.url AS document_url,
       d.process_code, d.save_status, d.doc_scope
FROM core.page_document pd
JOIN core.page p     ON p.id = pd.page_id
JOIN core.document d ON d.id = pd.document_id;
COMMENT ON VIEW saltcorn.page_document_link IS
    'core.page_document with page and document titles, for the Saltcorn curator app.';

-- ---------------------------------------------------------------- grants
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'birddog_ui') THEN
        RAISE NOTICE 'role birddog_ui missing; run deploy/db/init/02-ui.sh then re-run migrations';
        RETURN;
    END IF;
    EXECUTE 'GRANT USAGE ON SCHEMA saltcorn TO birddog_ui';
    EXECUTE 'GRANT SELECT ON saltcorn.search_hit, saltcorn.page_document_link TO birddog_ui';
    EXECUTE 'GRANT SELECT, INSERT ON saltcorn.search_request TO birddog_ui';
    EXECUTE 'GRANT USAGE ON SEQUENCE saltcorn.search_request_id_seq TO birddog_ui';
END $$;
