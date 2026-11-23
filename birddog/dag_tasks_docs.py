"""Task callables for dags/yaml/documents.yml: duplicate linking and FTP
matching. The work is in birddog.doc_matching; this module only cuts it into
batches for dynamic task mapping and turns "SFTP not configured" into a skip."""

# sha1 hashes per run, and per mapped task (a hash group is never split)
DUPE_HASHES_PER_RUN = 20000
DUPE_HASH_BATCH = 1000
# FTP files read over SFTP per run; one connection per batch, one batch at a
# time (max_active_tis_per_dag in the YAML), since JewishGen rate-limits logins
FTP_FILES_PER_RUN = 2000
FTP_FILE_BATCH = 100
# wiki files fingerprinted per run (two Range requests each)
FP_DOCS_PER_RUN = 2000
FP_DOC_BATCH = 50


def _chunks(items, size):
    return [items[i:i + size] for i in range(0, len(items), size)]


# --- duplicates ----------------------------------------------------------------

def dupe_hash_batches():
    from birddog.doc_matching import dupe_check_hashes
    return _chunks(dupe_check_hashes(DUPE_HASHES_PER_RUN), DUPE_HASH_BATCH)


def link_duplicate_batch(hashes):
    from birddog.doc_matching import link_duplicates
    return link_duplicates(hashes)


# --- FTP matching --------------------------------------------------------------

def ftp_fingerprint_batches():
    """Skips (with the rest of the SFTP branch) when SFTP_PASSWORD is unset;
    the wiki side and the reconcile still run on the fingerprints already stored."""
    from birddog.doc_matching import SFTPNotConfigured, ftp_files_to_fingerprint, sftp_config
    try:
        sftp_config()
    except SFTPNotConfigured as e:
        from airflow.sdk.exceptions import AirflowSkipException
        raise AirflowSkipException(f"FTP files are not read: {e}. See docs/onboarding/pipelines.md.")
    return _chunks(ftp_files_to_fingerprint(FTP_FILES_PER_RUN), FTP_FILE_BATCH)


def fingerprint_ftp_batch(files):
    from birddog.doc_matching import fingerprint_ftp_files
    return fingerprint_ftp_files(files)


def doc_fingerprint_batches():
    from birddog.doc_matching import documents_to_fingerprint
    return _chunks(documents_to_fingerprint(FP_DOCS_PER_RUN), FP_DOC_BATCH)


def fingerprint_doc_batch(docs):
    from birddog.doc_matching import fingerprint_documents
    return fingerprint_documents(docs)


def reconcile_ftp_matches():
    from birddog.doc_matching import reconcile_ftp_matches as run
    return run()
