-- Consumers for core.v_docs_need_dupe_check and core.v_ftp_need_match
-- (dags/yaml/documents.yml, birddog/doc_matching.py).
--
-- fp_error: why a content fingerprint could not be taken (file missing on the
-- wiki, unreadable on the SFTP server). A row with fp_error is not retried on
-- every run; the jewishgen_sftp transform clears it when the file changes.

ALTER TABLE core.document  ADD COLUMN IF NOT EXISTS fp_error text;
ALTER TABLE core.ftp_entry ADD COLUMN IF NOT EXISTS fp_error text;

-- matching joins FTP files to documents on byte size
CREATE INDEX IF NOT EXISTS document_byte_size ON core.document (byte_size) WHERE byte_size IS NOT NULL;
CREATE INDEX IF NOT EXISTS ftp_entry_size ON core.ftp_entry (size) WHERE type = 'file';
-- reverse lookups of the link tables (their primary keys lead with the other column)
CREATE INDEX IF NOT EXISTS ftp_document_doc ON core.ftp_document (document_id);
CREATE INDEX IF NOT EXISTS document_duplicate_dup ON core.document_duplicate (duplicate_id);
