-- jewishgen_sftp: landed SFTP listing -> core.ftp_entry.
-- Payload fields (birddog/sources/fetchers.py, _listing_record): path, filename,
-- folder, suffix, type (dir/file), size, mtime (ISO, UTC), depth.
-- A file whose size or mtime changed may have new bytes: its fingerprint is
-- cleared and it goes back on the match queue (core.v_ftp_need_match), which
-- the match_ftp_documents DAG (dags/yaml/documents.yml) works through.
INSERT INTO core.ftp_entry AS f (path, filename, folder, suffix, type, size, mtime, depth)
SELECT key,
    payload->>'filename', payload->>'folder', payload->>'suffix', payload->>'type',
    (payload->>'size')::bigint, (payload->>'mtime')::timestamptz, (payload->>'depth')::int
FROM landed
ON CONFLICT (path) DO UPDATE SET
    filename = EXCLUDED.filename, folder = EXCLUDED.folder, suffix = EXCLUDED.suffix,
    type = EXCLUDED.type, size = EXCLUDED.size, mtime = EXCLUDED.mtime, depth = EXCLUDED.depth,
    content_fp = CASE WHEN f.size IS DISTINCT FROM EXCLUDED.size OR f.mtime IS DISTINCT FROM EXCLUDED.mtime
                           OR f.type IS DISTINCT FROM EXCLUDED.type
                      THEN NULL ELSE f.content_fp END,
    match_checked = CASE WHEN f.size IS DISTINCT FROM EXCLUDED.size OR f.mtime IS DISTINCT FROM EXCLUDED.mtime
                              OR f.type IS DISTINCT FROM EXCLUDED.type
                         THEN false ELSE f.match_checked END,
    -- a file that couldn't be read is tried again once it changes (migration 014)
    fp_error = CASE WHEN f.size IS DISTINCT FROM EXCLUDED.size OR f.mtime IS DISTINCT FROM EXCLUDED.mtime
                         OR f.type IS DISTINCT FROM EXCLUDED.type
                    THEN NULL ELSE f.fp_error END;

-- Deleted entries (`gone`) stay: core.ftp_document links point at them, and
-- the legacy pruner's rules (dir -> file retypes, unrepresentable names) are
-- not ported yet. They show with in_source = false in views.source__jewishgen_sftp.
