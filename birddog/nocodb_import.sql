-- NocoDB (raw.nocodb_record) -> core. Run by birddog/nocodb_import.py
-- transform(); idempotent. Where a url appears twice, the most recently
-- updated record wins.

CREATE TEMP TABLE nc AS
    SELECT table_name, nocodb_id, fields AS f FROM raw.nocodb_record;
CREATE INDEX ON nc (table_name, nocodb_id);

CREATE OR REPLACE FUNCTION pg_temp.ts(v text) RETURNS timestamptz
    LANGUAGE sql IMMUTABLE AS $$ SELECT NULLIF(v, '')::timestamptz $$;
CREATE OR REPLACE FUNCTION pg_temp.num(v text) RETURNS bigint
    LANGUAGE sql IMMUTABLE AS $$ SELECT round(NULLIF(v, '')::numeric)::bigint $$;
-- NocoDB multi-selects come back either as a JSON array or a comma-separated string
CREATE OR REPLACE FUNCTION pg_temp.tags(v jsonb) RETURNS text[]
    LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE jsonb_typeof(v)
        WHEN 'array'  THEN ARRAY(SELECT jsonb_array_elements_text(v))
        WHEN 'string' THEN NULLIF(ARRAY(SELECT btrim(x) FROM unnest(string_to_array(v #>> '{}', ',')) x WHERE btrim(x) <> ''), '{}')
    END $$;
-- ids of the records in a NocoDB link list
CREATE OR REPLACE FUNCTION pg_temp.link_ids(v jsonb) RETURNS SETOF bigint
    LANGUAGE sql IMMUTABLE AS $$
    SELECT (e->>'id')::bigint FROM jsonb_array_elements(CASE WHEN jsonb_typeof(v) = 'array' THEN v ELSE '[]' END) e $$;

-- pages ----------------------------------------------------------------------
-- A page counts as fresh (not stale) only if Birddog refreshed it from the wiki
-- in the last 30 days; anything older or never refreshed is re-fetched by
-- refresh_pages, and wiki_changes is rewound to cover the fresh window.
INSERT INTO core.page (url, title, domain, label, root_label, seq_label, level,
                       native_description, description_en, years, wiki_timestamp,
                       availability, source_type, stale, last_birddog_update,
                       comments, reference_date, change_date, import_message, last_imported)
SELECT DISTINCT ON (f->>'url')
       f->>'url', COALESCE(f->>'title', ''), f->>'domain', f->>'label', f->>'root_label',
       f->>'seq_label', f->>'level', NULLIF(f->>'native_description', ''),
       NULLIF(f->>'description', ''), f->>'years', pg_temp.ts(f->>'timestamp'),
       f->>'availability', f->>'source_type',
       pg_temp.ts(f->>'last_birddog_update') IS NULL
           OR pg_temp.ts(f->>'last_birddog_update') < now() - interval '30 days',
       pg_temp.ts(f->>'last_birddog_update'),
       NULLIF(f->>'comments', ''), pg_temp.ts(f->>'reference_date')::date,
       pg_temp.ts(f->>'change_date')::date, NULLIF(f->>'import_message', ''),
       pg_temp.ts(f->>'last_imported')
FROM nc WHERE table_name = 'pages' AND f->>'url' IS NOT NULL
ORDER BY f->>'url', pg_temp.ts(f->>'UpdatedAt') DESC NULLS LAST
ON CONFLICT (url) DO UPDATE SET
    title = EXCLUDED.title, domain = EXCLUDED.domain, label = EXCLUDED.label,
    root_label = EXCLUDED.root_label, seq_label = EXCLUDED.seq_label, level = EXCLUDED.level,
    native_description = EXCLUDED.native_description, description_en = EXCLUDED.description_en,
    years = EXCLUDED.years, wiki_timestamp = EXCLUDED.wiki_timestamp,
    availability = EXCLUDED.availability, source_type = EXCLUDED.source_type,
    stale = EXCLUDED.stale, last_birddog_update = EXCLUDED.last_birddog_update,
    comments = EXCLUDED.comments, reference_date = EXCLUDED.reference_date,
    change_date = EXCLUDED.change_date, import_message = EXCLUDED.import_message,
    last_imported = EXCLUDED.last_imported;

CREATE TEMP TABLE page_map AS
    SELECT nc.nocodb_id, p.id FROM nc JOIN core.page p ON p.url = nc.f->>'url'
    WHERE nc.table_name = 'pages';
CREATE INDEX ON page_map (nocodb_id);

UPDATE core.page c SET parent_id = parent.id
FROM nc
JOIN LATERAL pg_temp.link_ids(nc.f->'parent') AS pid ON true
JOIN page_map parent ON parent.nocodb_id = pid
WHERE nc.table_name = 'pages' AND c.url = nc.f->>'url';

-- rewind the wiki change feed to the oldest fresh page, so edits made since
-- each page's last refresh are picked up
UPDATE raw.watermark SET value = to_char(
        (SELECT min(last_birddog_update) FROM core.page WHERE NOT stale AND last_birddog_update IS NOT NULL)
            AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
    updated_at = now()
WHERE source LIKE 'uk.wikisource.org:recentchanges:%'
  AND EXISTS (SELECT 1 FROM core.page WHERE NOT stale AND last_birddog_update IS NOT NULL)
  AND value > to_char((SELECT min(last_birddog_update) FROM core.page WHERE NOT stale AND last_birddog_update IS NOT NULL)
                      AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"');

-- documents ------------------------------------------------------------------
INSERT INTO core.document (url, title, domain, wikisource_url, filename,
    wiki_timestamp, byte_size, mimetype, mediatype, width, height, page_count,
    sha1_hash, description_url, content_fp, availability, lookup_status,
    location_status, dupes_checked, anomaly, anomaly_comment, last_birddog_update,
    description, process_code, doc_type, save_status, doc_structure, doc_scope,
    language, processor, pages_processed, when_transcribed, comments,
    reference_date, import_message, last_imported, old_page_description,
    old_doc_type, old_content_code, old_process_code)
SELECT DISTINCT ON (f->>'url')
    f->>'url', f->>'title', f->>'domain', NULLIF(f->>'wikisource_url', ''), NULLIF(f->>'filename', ''),
    pg_temp.ts(f->>'timestamp'), pg_temp.num(f->>'byte_size'), f->>'mimetype', f->>'mediatype',
    pg_temp.num(f->>'width'), pg_temp.num(f->>'height'), pg_temp.num(f->>'page_count'),
    NULLIF(f->>'sha1_hash', ''), NULLIF(f->>'description_url', ''), NULLIF(f->>'content_fp', ''),
    f->>'availability', f->>'lookup_status', f->>'location_status',
    COALESCE((f->>'dupes_checked')::boolean, false), COALESCE((f->>'anomaly')::boolean, false),
    NULLIF(f->>'anomaly_comment', ''), pg_temp.ts(f->>'last_birddog_update'),
    NULLIF(f->>'description', ''), f->>'process_code', pg_temp.tags(f->'doc_type'),
    f->>'save_status', f->>'doc_structure', f->>'doc_scope', f->>'language', f->>'processor',
    f->>'pages_processed', f->>'when_transcribed', NULLIF(f->>'comments', ''),
    pg_temp.ts(f->>'reference_date')::date, NULLIF(f->>'import_message', ''),
    pg_temp.ts(f->>'last_imported'), NULLIF(f->>'OLD_page_description', ''),
    f->>'OLD_doc_type', f->>'OLD_content_code', f->>'OLD_process_code'
FROM nc WHERE table_name = 'documents' AND f->>'url' IS NOT NULL
ORDER BY f->>'url', pg_temp.ts(f->>'UpdatedAt') DESC NULLS LAST
ON CONFLICT (url) DO UPDATE SET
    title = EXCLUDED.title, domain = EXCLUDED.domain, wikisource_url = EXCLUDED.wikisource_url,
    filename = EXCLUDED.filename, wiki_timestamp = EXCLUDED.wiki_timestamp,
    byte_size = EXCLUDED.byte_size, mimetype = EXCLUDED.mimetype, mediatype = EXCLUDED.mediatype,
    width = EXCLUDED.width, height = EXCLUDED.height, page_count = EXCLUDED.page_count,
    sha1_hash = EXCLUDED.sha1_hash, description_url = EXCLUDED.description_url,
    content_fp = EXCLUDED.content_fp, availability = EXCLUDED.availability,
    lookup_status = EXCLUDED.lookup_status, location_status = EXCLUDED.location_status,
    dupes_checked = EXCLUDED.dupes_checked, anomaly = EXCLUDED.anomaly,
    anomaly_comment = EXCLUDED.anomaly_comment, last_birddog_update = EXCLUDED.last_birddog_update,
    description = EXCLUDED.description, process_code = EXCLUDED.process_code,
    doc_type = EXCLUDED.doc_type, save_status = EXCLUDED.save_status,
    doc_structure = EXCLUDED.doc_structure, doc_scope = EXCLUDED.doc_scope,
    language = EXCLUDED.language, processor = EXCLUDED.processor,
    pages_processed = EXCLUDED.pages_processed, when_transcribed = EXCLUDED.when_transcribed,
    comments = EXCLUDED.comments, reference_date = EXCLUDED.reference_date,
    import_message = EXCLUDED.import_message, last_imported = EXCLUDED.last_imported,
    old_page_description = EXCLUDED.old_page_description, old_doc_type = EXCLUDED.old_doc_type,
    old_content_code = EXCLUDED.old_content_code, old_process_code = EXCLUDED.old_process_code;

CREATE TEMP TABLE doc_map AS
    SELECT nc.nocodb_id, d.id FROM nc JOIN core.document d ON d.url = nc.f->>'url'
    WHERE nc.table_name = 'documents';
CREATE INDEX ON doc_map (nocodb_id);

INSERT INTO core.page_document (page_id, document_id)
SELECT DISTINCT p.id, d.id
FROM nc
JOIN doc_map d ON d.nocodb_id = nc.nocodb_id
JOIN LATERAL pg_temp.link_ids(nc.f->'owning_pages') AS pid ON true
JOIN page_map p ON p.nocodb_id = pid
WHERE nc.table_name = 'documents'
ON CONFLICT DO NOTHING;

INSERT INTO core.document_duplicate (prevailing_id, duplicate_id)
SELECT DISTINCT d.id, dup.id
FROM nc
JOIN doc_map d ON d.nocodb_id = nc.nocodb_id
JOIN LATERAL pg_temp.link_ids(nc.f->'duplicates') AS did ON true
JOIN doc_map dup ON dup.nocodb_id = did
WHERE nc.table_name = 'documents' AND d.id <> dup.id
ON CONFLICT DO NOTHING;

-- locations ------------------------------------------------------------------
INSERT INTO core.location (location_id, modern_location_name, alternate_names,
    c1900_district, c1900_province, c1930_district, c1930_province,
    c1950_district, c1950_province, c2000_district, c2000_province)
SELECT DISTINCT ON (f->>'location_id')
    f->>'location_id', f->>'modern_location_name', f->>'alternate_names',
    f->>'c1900_district', f->>'c1900_province', f->>'c1930_district', f->>'c1930_province',
    f->>'c1950_district', f->>'c1950_province', f->>'c2000_district', f->>'c2000_province'
FROM nc WHERE table_name = 'locations' AND NULLIF(f->>'location_id', '') IS NOT NULL
ORDER BY f->>'location_id', nocodb_id DESC
ON CONFLICT (location_id) DO UPDATE SET
    modern_location_name = EXCLUDED.modern_location_name, alternate_names = EXCLUDED.alternate_names,
    c1900_district = EXCLUDED.c1900_district, c1900_province = EXCLUDED.c1900_province,
    c1930_district = EXCLUDED.c1930_district, c1930_province = EXCLUDED.c1930_province,
    c1950_district = EXCLUDED.c1950_district, c1950_province = EXCLUDED.c1950_province,
    c2000_district = EXCLUDED.c2000_district, c2000_province = EXCLUDED.c2000_province;

INSERT INTO core.document_location (document_id, location_id)
SELECT DISTINCT d.id, loc.f->>'location_id'
FROM nc
JOIN doc_map d ON d.nocodb_id = nc.nocodb_id
JOIN LATERAL pg_temp.link_ids(nc.f->'locations') AS lid ON true
JOIN nc loc ON loc.table_name = 'locations' AND loc.nocodb_id = lid
WHERE nc.table_name = 'documents' AND NULLIF(loc.f->>'location_id', '') IS NOT NULL
ON CONFLICT DO NOTHING;

-- ftp ------------------------------------------------------------------------
INSERT INTO core.ftp_entry (path, filename, folder, suffix, type, size, mtime, depth,
                            content_fp, match_checked)
SELECT DISTINCT ON (f->>'path')
    f->>'path', f->>'filename', f->>'folder', f->>'suffix', COALESCE(f->>'type', 'file'),
    pg_temp.num(f->>'size'),
    -- "YYYY-MM-DD HH:MM:SS" in UTC, without a zone
    (NULLIF(f->>'mtime', '')::timestamp AT TIME ZONE 'UTC'),
    pg_temp.num(f->>'depth')::int, NULLIF(f->>'content_fp', ''),
    COALESCE((f->>'match_checked')::boolean, false)
FROM nc WHERE table_name = 'ftp' AND NULLIF(f->>'path', '') IS NOT NULL
ORDER BY f->>'path', nocodb_id DESC
ON CONFLICT (path) DO UPDATE SET
    filename = EXCLUDED.filename, folder = EXCLUDED.folder, suffix = EXCLUDED.suffix,
    type = EXCLUDED.type, size = EXCLUDED.size, mtime = EXCLUDED.mtime, depth = EXCLUDED.depth,
    content_fp = EXCLUDED.content_fp, match_checked = EXCLUDED.match_checked;

INSERT INTO core.ftp_document (ftp_path, document_id)
SELECT DISTINCT nc.f->>'path', d.id
FROM nc
JOIN LATERAL pg_temp.link_ids(nc.f->'source_docs') AS did ON true
JOIN doc_map d ON d.nocodb_id = did
WHERE nc.table_name = 'ftp' AND NULLIF(nc.f->>'path', '') IS NOT NULL
ON CONFLICT DO NOTHING;

-- translation cache: existing English descriptions, keyed like pipeline.py
INSERT INTO core.translation (source_hash, source_text, translated_text)
SELECT DISTINCT ON (h) h, native_description, description_en
FROM (SELECT encode(sha256(convert_to(native_description, 'UTF8')), 'hex') AS h,
             native_description, description_en
      FROM core.page
      WHERE native_description <> '' AND description_en <> '') t
ON CONFLICT (source_hash) DO NOTHING;
