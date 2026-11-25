"""Task callables for the book DAGs (dags/yaml/books.yml): read_books (places;
the work is in birddog/books.py) and transcribe_books (full text and register
entries; birddog/book_transcripts.py).

Every step works on whatever is in core.book_read at the time: a run probes
all pending books, reads all approved ones (including books a person approved
in Mathesar since the last run) until the run's budget is spent, and assembles
every fully read book. So re-running the DAG is how approved books get read.
Claude calls run in the `llm_api` pool."""

import logging

_logger = logging.getLogger(__name__)

DEFAULT_BUDGET_USD = 10.0

try:
    from airflow.sdk.exceptions import AirflowSkipException
except ImportError:  # outside the Airflow image (tests)
    class AirflowSkipException(Exception):
        pass


def _int(value, default):
    try:
        return int(value) if str(value).strip() else default
    except (TypeError, ValueError):
        return default


def _float(value, default):
    try:
        return float(value) if str(value).strip() else default
    except (TypeError, ValueError):
        return default


def _run_id():
    try:
        from airflow.sdk import get_current_context
        return get_current_context()["run_id"]
    except Exception:
        return "manual"


def _require_key():
    from birddog.books import BookError, api_key
    try:
        api_key()
    except BookError as e:
        raise AirflowSkipException(str(e))


def enqueue_books(enqueue="", doc_type="Vitals", max_pages=""):
    from birddog.books import enqueue as enqueue_rows
    n = enqueue_rows(_int(enqueue, 0), doc_type=doc_type or "Vitals",
                     max_pages=_int(max_pages, None))
    _logger.info("queued %d %s books", n, doc_type)
    return n


def probe_books(auto_limit_tokens=""):
    from birddog.books import AUTO_LIMIT_TOKENS, probe_pending
    _require_key()
    summary = probe_pending(_run_id(), auto_limit=_int(auto_limit_tokens, AUTO_LIMIT_TOKENS))
    _logger.info("probe: %s", summary)
    return summary


def read_books(budget_usd=""):
    from birddog.books import read_approved
    _require_key()
    budget = _float(budget_usd, DEFAULT_BUDGET_USD)
    summary = read_approved(_run_id(), budget)
    _logger.info("read (budget $%.2f): %s", budget, summary)
    return summary


def assemble_books():
    from birddog.books import assemble_read
    _require_key()
    summary = assemble_read(_run_id())
    _logger.info("assemble: %s", summary)
    return summary


# --- transcribe_books ----------------------------------------------------------

DEFAULT_TRANSCRIBE_BUDGET_USD = 60.0


def _bool(value):
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def enqueue_transcripts(enqueue="", document_ids="", reread_names=""):
    """Queue books to transcribe, or with reread_names, put the finished books
    in document_ids back for the Opus names re-run (no Sonnet redo)."""
    from birddog.book_transcripts import enqueue as enqueue_rows, enqueue_names_reread
    ids = [int(x) for x in str(document_ids or "").replace(";", ",").split(",") if x.strip().isdigit()]
    if _bool(reread_names):
        n = enqueue_names_reread(ids)
        _logger.info("queued %d of %d books for the names re-read", n, len(ids))
        return n
    n = enqueue_rows(_int(enqueue, 0), ids)
    _logger.info("queued %d books for transcription", n)
    return n


def transcribe_books(budget_usd=""):
    from birddog.book_transcripts import transcribe_queued
    _require_key()
    budget = _float(budget_usd, DEFAULT_TRANSCRIBE_BUDGET_USD)
    summary = transcribe_queued(_run_id(), budget)
    _logger.info("transcribe (budget $%.2f): %s", budget, summary)
    return summary


def index_transcripts():
    from birddog.book_transcripts import index_transcribed
    summary = index_transcribed(_run_id())
    _logger.info("index: %s", summary)
    return summary


# --- guess_places (read_books, transcribe_books, guess_unmatched_places) ----------

DEFAULT_GUESS_BUDGET_USD = 1.0


def guess_places(budget_usd="", limit=""):
    """Rough positions for unmatched places (birddog/place_guess.py)."""
    from birddog.place_guess import guess_unmatched
    _require_key()
    budget = _float(budget_usd, DEFAULT_GUESS_BUDGET_USD)
    summary = guess_unmatched(_run_id(), budget, limit=_int(limit, None))
    _logger.info("guess_places (budget $%.2f): %s", budget, summary)
    return summary
