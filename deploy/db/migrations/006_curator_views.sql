-- Curator views: the curator-facing NocoDB grid views of the Birddog STAGE
-- base, recreated as Postgres views in schema `views` so they show up in
-- Mathesar (and any SQL tool). Source definitions: deploy/ui/nocodb_views.json;
-- mapping and caveats: deploy/ui/README.md, "Saved views".
--
-- Mathesar 0.12 shows Postgres views read-only (it edits rows through the
-- primary key, and views can't have one). Every view therefore starts with
-- the record's key (id / path / location_id), so curators can open the record
-- in its core table to edit it. deploy/ui/views.sh adds an "Editable version"
-- link to the view description (a pre-filtered core table) where the filter
-- only uses the table's own columns.
--
-- NocoDB field -> core mapping: Id -> id; description -> description_en;
-- timestamp -> wiki_timestamp; OLD_* -> old_*; link fields become the
-- linked records' display values joined with newlines, rollups become
-- counts. Fields copied from the owning page (label, root_label, seq_label,
-- level, years, page_description, page_native_description) come from the
-- document's owning page with the lowest id. NocoDB system fields (CreatedAt,
-- UpdatedAt, nc_*) and fields core doesn't store are left out.
--
-- Sort orders are not kept by views in Mathesar, so each view's NocoDB sort
-- is stated in its COMMENT; click the column header to apply it.
--
-- Idempotent; the migrate service re-runs it on every deploy. CREATE OR
-- REPLACE VIEW can only append columns: to drop, rename or reorder a view's
-- columns, DROP VIEW it first (and its dependants) in this file.

CREATE SCHEMA IF NOT EXISTS views;
COMMENT ON SCHEMA views IS 'Curator views recreated from the NocoDB STAGE base (read-only; see deploy/ui/README.md)';

-- ---------------------------------------------------------------- helpers
-- Columns shared by the Documents views. A plain view (not a function) so
-- the planner can push filters and LIMIT through it.
CREATE OR REPLACE VIEW views._document_base AS
SELECT d.id, d.url, d.title, d.domain, d.wikisource_url, d.filename, d.wiki_timestamp,
       d.byte_size, d.mimetype, d.mediatype, d.width, d.height, d.page_count, d.sha1_hash,
       d.description_url, d.content_fp, d.availability, d.lookup_status, d.location_status,
       d.dupes_checked, d.anomaly, d.anomaly_comment, d.last_birddog_update, d.description,
       d.process_code, d.doc_type, d.save_status, d.doc_structure, d.doc_scope, d.language,
       d.processor, d.pages_processed, d.when_transcribed, d.comments, d.reference_date,
       d.import_message, d.last_imported, d.old_page_description, d.old_doc_type,
       d.old_content_code, d.old_process_code,
       op.label, op.root_label, op.seq_label, op.level, op.years,
       op.page_description, op.page_native_description,
       (SELECT string_agg(p.title, E'\n' ORDER BY p.title)
          FROM core.page_document pd JOIN core.page p ON p.id = pd.page_id
         WHERE pd.document_id = d.id)                                    AS owning_pages,
       (SELECT count(*) FROM core.page_document pd
         WHERE pd.document_id = d.id)                                    AS num_owners,
       (SELECT string_agg(x.title, E'\n' ORDER BY x.title)
          FROM core.document_duplicate dd JOIN core.document x ON x.id = dd.duplicate_id
         WHERE dd.prevailing_id = d.id)                                  AS duplicates,
       (SELECT string_agg(x.title, E'\n' ORDER BY x.title)
          FROM core.document_duplicate dd JOIN core.document x ON x.id = dd.prevailing_id
         WHERE dd.duplicate_id = d.id)                                   AS duplicate_of,
       (SELECT count(*) FROM core.document_duplicate dd
         WHERE dd.prevailing_id = d.id)                                  AS num_duplicates,
       EXISTS (SELECT FROM core.document_duplicate dd
                WHERE dd.duplicate_id = d.id)                            AS is_duplicate,
       (SELECT string_agg(fd.ftp_path, E'\n' ORDER BY fd.ftp_path)
          FROM core.ftp_document fd WHERE fd.document_id = d.id)         AS ftp_items,
       (SELECT string_agg(dl.location_id, E'\n' ORDER BY dl.location_id)
          FROM core.document_location dl WHERE dl.document_id = d.id)    AS locations,
       (SELECT count(*) FROM core.document_location dl
         WHERE dl.document_id = d.id)                                    AS num_locations
FROM core.document d
LEFT JOIN LATERAL (
    SELECT p.label, p.root_label, p.seq_label, p.level, p.years,
           p.description_en AS page_description,
           p.native_description AS page_native_description
      FROM core.page_document pd JOIN core.page p ON p.id = pd.page_id
     WHERE pd.document_id = d.id
     ORDER BY p.id LIMIT 1
) op ON true;
COMMENT ON VIEW views._document_base IS
    'Helper for the documents__* views: core.document plus owning-page fields and link lookups.';

CREATE OR REPLACE VIEW views._page_base AS
SELECT p.id, p.url, p.title, p.domain, p.parent_id, p.label, p.root_label, p.seq_label,
       p.level, p.native_description, p.description_en, p.years, p.wiki_timestamp,
       p.availability, p.source_type, p.stale, p.last_birddog_update, p.comments,
       p.reference_date, p.change_date, p.import_message, p.last_imported,
       (SELECT pp.title FROM core.page pp WHERE pp.id = p.parent_id)    AS parent,
       (SELECT count(*) FROM core.page c WHERE c.parent_id = p.id)      AS children_count,
       (SELECT string_agg(d.title, E'\n' ORDER BY d.title)
          FROM core.page_document pd JOIN core.document d ON d.id = pd.document_id
         WHERE pd.page_id = p.id)                                       AS doc_links,
       (SELECT count(*) FROM core.page_document pd
         WHERE pd.page_id = p.id)                                       AS doc_links_count
FROM core.page p;
COMMENT ON VIEW views._page_base IS
    'Helper for the pages__* views: core.page plus parent title, child count and linked documents.';

-- ---------------------------------------------------------------- Pages
CREATE OR REPLACE VIEW views.pages__all AS
SELECT title, id, label, root_label, seq_label, url, domain, description_en, native_description,
       wiki_timestamp, last_imported, last_birddog_update, reference_date, availability, comments,
       level, change_date, years, source_type, import_message, children_count, parent_id, parent,
       doc_links
FROM views._page_base;
COMMENT ON VIEW views.pages__all IS
    'NocoDB Pages / "All": all records and fields. Filter: none. Sort: id ascending.';

CREATE OR REPLACE VIEW views.pages__recently_updated AS
SELECT title, id, label, url, children_count, parent_id, parent, doc_links,
       last_birddog_update, last_imported
FROM views._page_base
WHERE GREATEST(last_birddog_update, last_imported) >= now() - interval '1 day';
COMMENT ON VIEW views.pages__recently_updated IS
    'NocoDB Pages / "Recently Updated": records created or updated in the last day. '
    'Core has no row-modified timestamp, so "updated" means last_birddog_update or '
    'last_imported within the last day (curator edits are not tracked). '
    'Sort: unknown (the NocoDB view was empty at export); last_birddog_update descending suggested.';

CREATE OR REPLACE VIEW views.pages__cdiak AS
SELECT title, id, label, root_label, seq_label, url, domain, description_en, native_description,
       wiki_timestamp, last_imported, last_birddog_update, reference_date, availability, comments,
       level, change_date, years, source_type, import_message, children_count, parent_id, parent,
       doc_links, doc_links_count
FROM views._page_base
WHERE root_label = 'CDIAK';
COMMENT ON VIEW views.pages__cdiak IS
    'NocoDB Pages / "CDIAK": pages of the CDIAK archive. Filter: root_label = CDIAK. '
    'Sort: seq_label ascending.';

CREATE OR REPLACE VIEW views.pages__juliana_test AS
SELECT title, id, root_label, level, label, url, description_en, domain, availability, comments,
       years, source_type, import_message, children_count, parent_id, parent, doc_links,
       doc_links_count
FROM views._page_base
WHERE root_label = 'DACHVO' AND level IN ('archive', 'fond', 'opus');
COMMENT ON VIEW views.pages__juliana_test IS
    'NocoDB Pages / "Juliana test": DACHVO archive, fond and opus pages. '
    'Filter: root_label = DACHVO and level in (archive, fond, opus). Sort: label ascending.';

CREATE OR REPLACE VIEW views.pages__import_messages AS
SELECT title, id, label, import_message, url, children_count, parent_id, parent, doc_links,
       last_imported, last_birddog_update
FROM views._page_base
WHERE import_message <> '';
COMMENT ON VIEW views.pages__import_messages IS
    'NocoDB Pages / "Import Messages": pages with a spreadsheet import message. '
    'Filter: import_message is not blank. Sort: NocoDB used UpdatedAt descending, which core '
    'does not store; use last_imported descending.';

-- ---------------------------------------------------------------- Documents
CREATE OR REPLACE VIEW views.documents__all AS
SELECT title, id, label, root_label, seq_label, level, url, domain, description,
       page_description, page_native_description, years, filename, pages_processed,
       old_doc_type, old_content_code, old_process_code, comments, byte_size, mimetype,
       mediatype, width, height, page_count, sha1_hash, description_url, availability,
       import_message, wiki_timestamp, last_imported, last_birddog_update, reference_date,
       when_transcribed, language, processor, lookup_status, dupes_checked, owning_pages,
       duplicates, duplicate_of, locations, num_owners, num_duplicates, num_locations,
       wikisource_url
FROM views._document_base;
COMMENT ON VIEW views.documents__all IS
    'NocoDB Documents / "All": all records. Filter: none. Sort: id ascending.';

CREATE OR REPLACE VIEW views.documents__recently_updated AS
SELECT title, id, label, url, page_description, owning_pages, duplicates, ftp_items, content_fp,
       locations, num_locations, wikisource_url, last_birddog_update, last_imported
FROM views._document_base
WHERE GREATEST(last_birddog_update, last_imported) >= now() - interval '1 day';
COMMENT ON VIEW views.documents__recently_updated IS
    'NocoDB Documents / "Recently Updated": records created or updated in the last day. '
    'Core has no row-modified timestamp, so "updated" means last_birddog_update or '
    'last_imported within the last day (curator edits are not tracked). '
    'Sort: unknown (the NocoDB view was empty at export); last_birddog_update descending suggested.';

CREATE OR REPLACE VIEW views.documents__multi_owner AS
SELECT title, id, label, url, owning_pages, num_owners, is_duplicate, ftp_items, content_fp,
       locations, num_locations, wikisource_url, location_status, doc_type, doc_structure,
       doc_scope, process_code, save_status, anomaly, anomaly_comment, old_page_description
FROM views._document_base
WHERE num_owners > 1;
COMMENT ON VIEW views.documents__multi_owner IS
    'NocoDB Documents / "Multi-Owner": documents linked from more than one page. '
    'Filter: num_owners > 1. Sort: id ascending.';

CREATE OR REPLACE VIEW views.documents__orphans AS
SELECT title, id, label, url, is_duplicate, ftp_items, content_fp, locations, num_locations,
       wikisource_url, location_status, doc_type, doc_structure, doc_scope, process_code,
       save_status, anomaly, anomaly_comment, old_page_description
FROM views._document_base
WHERE num_owners = 0;
COMMENT ON VIEW views.documents__orphans IS
    'NocoDB Documents / "Orphans": documents no page links to. '
    'Filter: num_owners = 0. Sort: id ascending.';

CREATE OR REPLACE VIEW views.documents__has_duplicates AS
SELECT title, id, label, url, owning_pages, duplicates, num_duplicates, is_duplicate, ftp_items,
       content_fp, locations, num_locations, wikisource_url, location_status, doc_type,
       doc_structure, doc_scope, process_code, save_status, anomaly, anomaly_comment,
       old_page_description
FROM views._document_base
WHERE num_duplicates > 0;
COMMENT ON VIEW views.documents__has_duplicates IS
    'NocoDB Documents / "Has Duplicates": documents other documents duplicate. '
    'Filter: num_duplicates > 0. Sort: id ascending.';

CREATE OR REPLACE VIEW views.documents__cdiak AS
SELECT title, id, label, root_label, seq_label, level, url, domain, description,
       page_description, page_native_description, years, filename, pages_processed,
       old_doc_type, old_content_code, old_process_code, comments, byte_size, mimetype,
       mediatype, width, height, page_count, sha1_hash, description_url, availability,
       import_message, wiki_timestamp, last_imported, last_birddog_update, reference_date,
       when_transcribed, language, processor, lookup_status, dupes_checked, owning_pages,
       duplicates, duplicate_of, locations, num_owners, num_duplicates, num_locations,
       wikisource_url, location_status, doc_type, doc_structure, doc_scope, process_code,
       save_status, anomaly, anomaly_comment, old_page_description
FROM views._document_base;
COMMENT ON VIEW views.documents__cdiak IS
    'NocoDB Documents / "CDIAK": despite its name the NocoDB view had no filter (it listed '
    'all documents), so neither does this one. Sort: label ascending.';

-- ---------------------------------------------------------------- FTP Site
CREATE OR REPLACE VIEW views.ftp_site__all AS
SELECT f.path, f.first_seen, f.folder, f.suffix, f.filename, f.content_fp, f.type, f.size,
       f.depth, f.mtime, f.match_checked,
       (SELECT string_agg(d.title, E'\n' ORDER BY d.title)
          FROM core.ftp_document fd JOIN core.document d ON d.id = fd.document_id
         WHERE fd.ftp_path = f.path)                                    AS source_docs,
       EXISTS (SELECT FROM core.ftp_document fd WHERE fd.ftp_path = f.path) AS has_match
FROM core.ftp_entry f;
COMMENT ON VIEW views.ftp_site__all IS
    'NocoDB FTP Site / "All": all FTP entries. Filter: none. Sort: first_seen ascending '
    '(NocoDB: Id ascending). Read-only for curators anyway (pipeline-owned).';

-- ---------------------------------------------------------------- Locations
CREATE OR REPLACE VIEW views.locations__locations AS
SELECT l.location_id, l.modern_location_name, l.alternate_names,
       l.c1900_district, l.c1900_province, l.c1930_district, l.c1930_province,
       l.c1950_district, l.c1950_province, l.c2000_district, l.c2000_province,
       (SELECT string_agg(d.title, E'\n' ORDER BY d.title)
          FROM core.document_location dl JOIN core.document d ON d.id = dl.document_id
         WHERE dl.location_id = l.location_id)                          AS documents,
       (SELECT count(*) FROM core.document_location dl
         WHERE dl.location_id = l.location_id)                          AS num_documents
FROM core.location l;
COMMENT ON VIEW views.locations__locations IS
    'NocoDB Locations / "Locations": all locations. Filter: none. Sort: location_id ascending '
    '(NocoDB: Id ascending). region, jewish_population, notes, latitude, longitude and the '
    '*_town / *_country fields are not stored in core.location.';

CREATE OR REPLACE VIEW views.locations__has_documents AS
SELECT location_id, modern_location_name, documents, num_documents
FROM views.locations__locations
WHERE num_documents > 0;
COMMENT ON VIEW views.locations__has_documents IS
    'NocoDB Locations / "Has Documents": locations linked to at least one document. '
    'Filter: num_documents > 0. Sort: location_id ascending. region is not stored in core.';

-- ---------------------------------------------------------------- grants
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'birddog_ui') THEN
        RAISE NOTICE 'role birddog_ui missing; run deploy/db/init/02-ui.sh then re-run migrations';
        RETURN;
    END IF;
    EXECUTE 'GRANT USAGE ON SCHEMA views TO birddog_ui';
    EXECUTE 'GRANT SELECT ON ALL TABLES IN SCHEMA views TO birddog_ui';
    EXECUTE 'REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON ALL TABLES IN SCHEMA views FROM birddog_ui';
END $$;
