-- core: the system of record, replacing the NocoDB tables (Pages, Documents,
-- FTP Site, Locations) and their link fields. Copies NocoDB kept for display
-- (Documents.label/years/page_description copied from the owning page) are
-- not stored twice: join through core.page_document instead.

CREATE TABLE IF NOT EXISTS core.archive_root (
    title       text PRIMARY KEY,
    label       text NOT NULL,
    description text NOT NULL DEFAULT '',
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS core.page (
    id                  bigserial PRIMARY KEY,
    url                 text NOT NULL UNIQUE,
    title               text NOT NULL,
    domain              text,
    parent_id           bigint REFERENCES core.page (id) ON DELETE SET NULL,
    label               text,
    root_label          text,
    seq_label           text,
    level               text,               -- archive / fond / opus / ...
    native_description  text,               -- Ukrainian, from the wiki template
    description_en      text,               -- machine translation of native_description
    years               text,
    wiki_timestamp      timestamptz,        -- last wiki revision
    availability        text,               -- linked / unlinked / redlinked / error
    source_type         text,               -- wiki / archive
    stale               boolean NOT NULL DEFAULT true,   -- needs a refresh from the wiki
    last_birddog_update timestamptz,
    -- from the legacy spreadsheet import
    comments            text,
    reference_date      date,
    change_date         date,
    import_message      text,
    last_imported       timestamptz
);
CREATE INDEX IF NOT EXISTS page_parent ON core.page (parent_id);
CREATE INDEX IF NOT EXISTS page_title ON core.page (title);

CREATE TABLE IF NOT EXISTS core.document (
    id                  bigserial PRIMARY KEY,
    url                 text NOT NULL UNIQUE,
    title               text,
    domain              text,
    wikisource_url      text,
    filename            text,
    -- file metadata from MediaWiki
    wiki_timestamp      timestamptz,
    byte_size           bigint,
    mimetype            text,
    mediatype           text,
    width               int,
    height              int,
    page_count          int,
    sha1_hash           text,
    description_url     text,
    content_fp          text,               -- fingerprint for FTP matching
    -- status
    availability        text,
    lookup_status       text,               -- valid / invalid
    location_status     text,               -- pending / found / none / failed
    dupes_checked       boolean NOT NULL DEFAULT false,
    anomaly             boolean NOT NULL DEFAULT false,
    anomaly_comment     text,
    last_birddog_update timestamptz,
    -- curation workflow
    description         text,               -- spreadsheet description (English)
    process_code        text,
    doc_type            text[],
    save_status         text,
    doc_structure       text,
    doc_scope           text,
    language            text,
    processor           text,
    pages_processed     text,
    when_transcribed    text,
    comments            text,
    reference_date      date,
    import_message      text,
    last_imported       timestamptz,
    -- legacy codes, kept as imported
    old_page_description text,
    old_doc_type        text,
    old_content_code    text,
    old_process_code    text
);
CREATE INDEX IF NOT EXISTS document_sha1 ON core.document (sha1_hash);
CREATE INDEX IF NOT EXISTS document_content_fp ON core.document (content_fp);

CREATE TABLE IF NOT EXISTS core.page_document (
    page_id     bigint NOT NULL REFERENCES core.page (id) ON DELETE CASCADE,
    document_id bigint NOT NULL REFERENCES core.document (id) ON DELETE CASCADE,
    PRIMARY KEY (page_id, document_id)
);
CREATE INDEX IF NOT EXISTS page_document_doc ON core.page_document (document_id);

-- duplicate_id is a duplicate of prevailing_id (NocoDB: duplicates / duplicate_of)
CREATE TABLE IF NOT EXISTS core.document_duplicate (
    prevailing_id bigint NOT NULL REFERENCES core.document (id) ON DELETE CASCADE,
    duplicate_id  bigint NOT NULL REFERENCES core.document (id) ON DELETE CASCADE,
    PRIMARY KEY (prevailing_id, duplicate_id)
);

CREATE TABLE IF NOT EXISTS core.location (
    location_id          text PRIMARY KEY,
    modern_location_name text,
    alternate_names      text,
    c1900_district text, c1900_province text,
    c1930_district text, c1930_province text,
    c1950_district text, c1950_province text,
    c2000_district text, c2000_province text
);

CREATE TABLE IF NOT EXISTS core.document_location (
    document_id bigint NOT NULL REFERENCES core.document (id) ON DELETE CASCADE,
    location_id text   NOT NULL REFERENCES core.location (location_id),
    PRIMARY KEY (document_id, location_id)
);

-- translation cache: one row per distinct source text, reused across pages
CREATE TABLE IF NOT EXISTS core.translation (
    source_hash     text PRIMARY KEY,        -- sha256 of source_text
    source_text     text NOT NULL,
    target_lang     text NOT NULL DEFAULT 'en',
    translated_text text NOT NULL,
    translated_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS core.ftp_entry (
    path          text PRIMARY KEY,
    filename      text,
    folder        text,
    suffix        text,
    type          text NOT NULL,             -- dir / file
    size          bigint,
    mtime         timestamptz,
    depth         int,
    content_fp    text,
    match_checked boolean NOT NULL DEFAULT false,
    first_seen    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS core.ftp_document (
    ftp_path    text   NOT NULL REFERENCES core.ftp_entry (path) ON DELETE CASCADE,
    document_id bigint NOT NULL REFERENCES core.document (id) ON DELETE CASCADE,
    PRIMARY KEY (ftp_path, document_id)
);

-- work-queue views, replacing the NocoDB "BD:*" views the loops polled
CREATE OR REPLACE VIEW core.v_pages_stale AS
    SELECT id, url, title FROM core.page WHERE stale;

CREATE OR REPLACE VIEW core.v_untranslated AS        -- BD:Untranslated
    SELECT id, url, native_description FROM core.page
    WHERE native_description <> '' AND description_en IS NULL;

CREATE OR REPLACE VIEW core.v_docs_no_meta AS        -- BD:Missing Metadata
    SELECT id, url, title FROM core.document
    WHERE sha1_hash IS NULL AND domain IS NOT NULL;

CREATE OR REPLACE VIEW core.v_docs_need_dupe_check AS  -- BD:Need Dupe Check
    SELECT id, url, sha1_hash FROM core.document
    WHERE NOT dupes_checked AND sha1_hash IS NOT NULL;

CREATE OR REPLACE VIEW core.v_docs_no_location AS    -- BD:Need Locations
    SELECT id, url, description FROM core.document
    WHERE location_status = 'pending';

CREATE OR REPLACE VIEW core.v_ftp_need_match AS      -- BD:Need Match Check
    SELECT path, size, content_fp FROM core.ftp_entry
    WHERE type = 'file' AND NOT match_checked;

-- app: user-facing state, replacing the per-user KV namespaces

CREATE TABLE IF NOT EXISTS app.user_account (
    email         text PRIMARY KEY,
    name          text NOT NULL,
    password_hash text NOT NULL,
    role          text NOT NULL DEFAULT 'user',
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS app.user_preference (
    email  text  NOT NULL REFERENCES app.user_account (email) ON DELETE CASCADE,
    key    text  NOT NULL,
    value  jsonb NOT NULL,
    PRIMARY KEY (email, key)
);

-- one row per watched scope (watcher "header")
CREATE TABLE IF NOT EXISTS app.watch (
    email             text NOT NULL REFERENCES app.user_account (email) ON DELETE CASCADE,
    archive_title     text NOT NULL,
    include           text[] NOT NULL,
    exclude           text[] NOT NULL DEFAULT '{}',
    cutoff_date       timestamptz NOT NULL,
    last_checked_date timestamptz NOT NULL,
    moved_to          text,
    PRIMARY KEY (email, archive_title)
);

-- per-item alert state inside a watch: unresolved entries plus resolve history
CREATE TABLE IF NOT EXISTS app.alert_state (
    email         text NOT NULL,
    archive_title text NOT NULL,
    item_title    text NOT NULL,
    resolved      boolean NOT NULL DEFAULT false,
    modified      timestamptz,
    last_resolved timestamptz,
    significance  text,                       -- insignificant / new_page / null
    sig_span      jsonb,
    entry         jsonb NOT NULL DEFAULT '{}', -- remaining watcher fields as stored today
    PRIMARY KEY (email, archive_title, item_title),
    FOREIGN KEY (email, archive_title) REFERENCES app.watch (email, archive_title) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS alert_state_unresolved ON app.alert_state (email, archive_title) WHERE NOT resolved;
