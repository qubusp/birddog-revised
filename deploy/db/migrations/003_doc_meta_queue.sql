-- Only wiki-hosted files have MediaWiki metadata to fetch, and a file the API
-- couldn't resolve (availability 'error') leaves the queue instead of being
-- retried on every run.
CREATE OR REPLACE VIEW core.v_docs_no_meta AS
    SELECT id, url, title FROM core.document
    WHERE sha1_hash IS NULL
      AND availability IS DISTINCT FROM 'error'
      AND (domain = 'commons.wikimedia.org' OR domain = 'uk.wikisource.org');
