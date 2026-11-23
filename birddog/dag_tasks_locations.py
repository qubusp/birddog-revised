"""Task callables for the location finder DAG (dags/yaml/locations.yml).

`locate_documents` drains core.v_docs_no_location (documents with
location_status = 'pending'): new documents get there from
refresh_doc_metadata, and app_refresh_page's update_locations re-queues a
page's (or subtree's) documents. The work is in birddog/locations/pg_store.py.

The LLM key (GROQ_LOCATION_KEY for the groq provider) must be in the Airflow
environment. Without it both tasks skip, and the documents stay pending until
the key is set. LLM calls run in the `llm_api` pool (1 slot), so only one
batch talks to the provider at a time."""

import logging

_logger = logging.getLogger(__name__)

# documents per DAG run, split into mapped batches; at Groq free-tier limits a
# batch of 50 takes a few minutes (5 calls of 10 documents, plus fallback calls)
DOCS_PER_RUN = 300
DOC_BATCH = 50

try:
    from airflow.sdk.exceptions import AirflowSkipException
except ImportError:  # outside the Airflow image (tests)
    class AirflowSkipException(Exception):
        pass


def _skip_without_key():
    from birddog.locations.pg_store import missing_key
    missing = missing_key()
    if missing:
        raise AirflowSkipException(f"{missing} is not set; documents stay pending")


def _int(value, default):
    try:
        return int(value) if str(value).strip() else default
    except (TypeError, ValueError):
        return default


def pending_location_batches(limit=""):
    """Up to `limit` (default DOCS_PER_RUN) pending document ids, in batches."""
    from birddog.locations.pg_store import pending_count, pending_document_ids
    _skip_without_key()
    ids = pending_document_ids(_int(limit, DOCS_PER_RUN))
    _logger.info("location queue: %d pending, taking %d", pending_count(), len(ids))
    return [ids[i:i + DOC_BATCH] for i in range(0, len(ids), DOC_BATCH)]


def _last_attempt():
    """True on the task's final try (or outside Airflow)."""
    try:
        from airflow.sdk import get_current_context
        ti = get_current_context()["ti"]
        return ti.try_number > ti.max_tries
    except Exception:
        return True


def locate_batch(doc_ids):
    from birddog.locations.pg_store import locate_documents
    _skip_without_key()
    summary = locate_documents(list(doc_ids), last_attempt=_last_attempt())
    _logger.info("locate_batch: %s", summary)
    return summary
