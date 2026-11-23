"""Postgres side of location inference: what LocationUpdateManager did against
NocoDB, done against core.* for the `locate_documents` DAG
(dags/yaml/locations.yml, birddog/dag_tasks_locations.py).

- PgRecords gives FileLocationFinder the NocoDB-shaped records it reads
  (Documents, Pages, Locations) from core.document, core.page,
  core.page_document and core.location.
- CachedTranslator translates a document title's Cyrillic part through the
  core.translation cache; with translation switched off it returns "" and the
  title's translated part is left out instead of failing the document.
- locate_documents() runs the finder over a list of document ids, a chunk at
  a time, and writes each chunk's outcome before starting the next:
  core.document_location links and core.document.location_status
  (found / none / failed). Documents no longer pending are skipped, so a
  retried task does not redo finished chunks.

The extraction and matching logic is unchanged (birddog/locations/finder.py,
extractor.py, matcher.py)."""

import hashlib
import logging
import os

from birddog import pg

_logger = logging.getLogger(__name__)

PROVIDER = "groq"
# documents per LLM call; the extractor caps a call at 10 (4k-token context)
CHUNK_SIZE = 10


def provider_key_env(provider=PROVIDER):
    from birddog.locations.extractor import PROVIDER_KEY_ENV
    return PROVIDER_KEY_ENV[provider]


def missing_key(provider=PROVIDER):
    """The name of the provider's API key variable if it is unset, else None."""
    name = provider_key_env(provider)
    return None if os.environ.get(name) else name


# --- reads -------------------------------------------------------------------

def pending_document_ids(limit):
    """Oldest documents waiting in core.v_docs_no_location."""
    with pg.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM core.v_docs_no_location ORDER BY id LIMIT %s", (limit,))
        return [r[0] for r in cur.fetchall()]


def pending_count():
    with pg.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM core.v_docs_no_location")
        return cur.fetchone()[0]


class PgRecords:
    """The subset of birddog.database.Database that FileLocationFinder and
    LocationMatcher use, read from core.*. Records keep NocoDB's field names:

    Documents: Id, title, description, comments, page_description (English
        description of the first owning page), owning_pages [{Id, title}]
    Pages: Id, title, root_label, description (description_en), parent [{Id}]
    Locations: location_id, modern_location_name, alternate_names, c*_district,
        c*_province

    Reads go through one connection, opened on first use; close() it."""

    def __init__(self, conn=None):
        self._conn = conn
        self._own_conn = conn is None
        self._pages = {}

    def _cursor(self):
        if self._conn is None:
            import psycopg2
            self._conn = psycopg2.connect(pg.database_url())
            self._conn.set_session(readonly=True, autocommit=True)
        return self._conn.cursor()

    def close(self):
        if self._own_conn and self._conn is not None:
            self._conn.close()
            self._conn = None

    def read(self, table, record_id):
        if table == "Documents":
            return self._document(record_id)
        if table == "Pages":
            return self._page(record_id)
        raise ValueError(f"PgRecords.read: unsupported table {table}")

    def scan_all(self, table, **_kwargs):
        if table != "Locations":
            raise ValueError(f"PgRecords.scan_all: unsupported table {table}")
        with self._cursor() as cur:
            cur.execute("""SELECT location_id, modern_location_name, alternate_names,
                                  c1900_district, c1900_province, c1930_district, c1930_province,
                                  c1950_district, c1950_province, c2000_district, c2000_province
                           FROM core.location ORDER BY location_id""")
            columns = [c[0] for c in cur.description]
            return [dict(zip(columns, row)) for row in cur.fetchall()]

    def _document(self, doc_id):
        with self._cursor() as cur:
            cur.execute("SELECT id, title, description, comments FROM core.document WHERE id = %s",
                        (doc_id,))
            row = cur.fetchone()
            if row is None:
                return {}
            cur.execute("""SELECT p.id, p.title, p.description_en FROM core.page_document pd
                           JOIN core.page p ON p.id = pd.page_id
                           WHERE pd.document_id = %s ORDER BY p.id""", (doc_id,))
            owners = cur.fetchall()
        return {
            "Id": row[0],
            "title": row[1],
            "description": row[2],
            "comments": row[3],
            "page_description": owners[0][2] if owners else None,
            "owning_pages": [{"Id": p[0], "title": p[1]} for p in owners] or None,
        }

    def _page(self, page_id):
        if page_id not in self._pages:
            with self._cursor() as cur:
                cur.execute("""SELECT id, title, root_label, description_en, parent_id
                               FROM core.page WHERE id = %s""", (page_id,))
                row = cur.fetchone()
            # the finder treats a missing page as a page with nothing to add
            self._pages[page_id] = {} if row is None else {
                "Id": row[0],
                "title": row[1],
                "root_label": row[2],
                "description": row[3],
                "parent": [{"Id": row[4]}] if row[4] is not None else None,
            }
        return self._pages[page_id]


class CachedTranslator:
    """Translates through the core.translation cache (shared with the
    translate DAG), calling birddog.translate only on a miss. When
    translation is switched off in this environment it returns "", and the
    finder works without the title's translated part; `disabled` counts how
    often that happened."""

    def __init__(self):
        self.disabled = 0
        self.cached = 0
        self.translated = 0

    def __call__(self, text):
        from birddog.translate import TranslationDisabledError, translation
        key = hashlib.sha256(text.encode("utf-8")).hexdigest()
        with pg.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT translated_text FROM core.translation WHERE source_hash = %s", (key,))
            row = cur.fetchone()
        if row:
            self.cached += 1
            return row[0]
        try:
            result = translation(text)
        except TranslationDisabledError:
            self.disabled += 1
            return ""
        with pg.connect() as conn, conn.cursor() as cur:
            cur.execute("""INSERT INTO core.translation (source_hash, source_text, translated_text)
                           VALUES (%s, %s, %s) ON CONFLICT (source_hash) DO NOTHING""",
                        (key, text, result))
        self.translated += 1
        return result


def make_finder(records, translator, extractor=None, provider=PROVIDER):
    from birddog.locations.finder import FileLocationFinder
    return FileLocationFinder(records, provider, translate=translator, extractor=extractor)


# --- writes ------------------------------------------------------------------

def _still_pending(cur, doc_ids):
    cur.execute("SELECT id FROM core.document WHERE id = ANY(%s) AND location_status = 'pending'",
                (list(doc_ids),))
    pending = {r[0] for r in cur.fetchall()}
    return [i for i in doc_ids if i in pending]


def write_results(results, summary):
    """Writes the finder's results ({doc_id: (error, location_ids, ...)}) in
    one transaction. A document whose inference failed or found nothing keeps
    its existing links (as in LocationUpdateManager); a found one has its
    links replaced. location_ids not in core.location are dropped; if none
    remain the document is marked failed."""
    with pg.connect() as conn, conn.cursor() as cur:
        wanted = {str(loc) for r in results.values() if not r[0] for loc in r[1]}
        cur.execute("SELECT location_id FROM core.location WHERE location_id = ANY(%s)", (list(wanted),))
        known = {r[0] for r in cur.fetchall()}
        for doc_id, result in results.items():
            error, location_ids = result[0], [str(loc) for loc in result[1]]
            if error:
                status = "failed"
                summary["failed"] += 1
            elif not location_ids:
                status = "none"
                summary["none"] += 1
            else:
                targets = sorted({loc for loc in location_ids if loc in known})
                unknown = sorted(set(location_ids) - known)
                if unknown:
                    _logger.warning("document %s: unknown location_id(s) %s", doc_id, unknown)
                if not targets:
                    status = "failed"
                    summary["failed"] += 1
                else:
                    cur.execute("""DELETE FROM core.document_location
                                   WHERE document_id = %s AND NOT (location_id = ANY(%s))""",
                                (doc_id, targets))
                    removed = cur.rowcount
                    cur.execute("""INSERT INTO core.document_location (document_id, location_id)
                                   SELECT %s, unnest(%s::text[]) ON CONFLICT DO NOTHING""",
                                (doc_id, targets))
                    added = cur.rowcount
                    status = "found"
                    summary["found"] += 1
                    summary["links_added"] += added
                    summary["links_removed"] += removed
            cur.execute("""UPDATE core.document SET location_status = %s
                           WHERE id = %s""", (status, doc_id))


def mark_failed(doc_ids):
    with pg.connect() as conn, conn.cursor() as cur:
        cur.execute("""UPDATE core.document SET location_status = 'failed'
                       WHERE id = ANY(%s) AND location_status = 'pending'""", (list(doc_ids),))
        return cur.rowcount


# --- run ---------------------------------------------------------------------

def locate_documents(doc_ids, finder=None, translator=None, last_attempt=True,
                     chunk_size=CHUNK_SIZE):
    """Infers locations for the given documents, CHUNK_SIZE at a time, and
    writes each chunk's results before the next (so a long batch keeps its
    progress). Returns a summary.

    - A rate limit longer than the extractor waits for (RateLimitedError)
      stops the batch: the rest stay pending for the next run.
    - Any other error in a chunk is raised, so Airflow retries the task,
      unless last_attempt: then the chunk's documents are marked failed (a
      document that always breaks the finder must not block the queue head
      forever) and the batch goes on."""
    from birddog.fetch import RateLimitedError

    summary = {"docs": len(doc_ids), "skipped_not_pending": 0, "found": 0, "none": 0,
               "failed": 0, "links_added": 0, "links_removed": 0, "chunk_errors": 0,
               "rate_limited": False, "left_pending": 0, "title_untranslated": 0}
    with pg.connect() as conn, conn.cursor() as cur:
        todo = _still_pending(cur, doc_ids)
    summary["skipped_not_pending"] = len(doc_ids) - len(todo)
    if not todo:
        return summary

    records = PgRecords()
    translator = translator or CachedTranslator()
    try:
        finder = finder or make_finder(records, translator)
        for start in range(0, len(todo), chunk_size):
            # re-check: a concurrent run or a curator may have handled some
            with pg.connect() as conn, conn.cursor() as cur:
                chunk = _still_pending(cur, todo[start:start + chunk_size])
            if not chunk:
                continue
            try:
                results = finder.get_doc_locations_batched(
                    chunk, batch_size=chunk_size, only_smallest_locations=True)
            except RateLimitedError as err:
                summary["rate_limited"] = True
                summary["retry_after_s"] = err.retry_after
                summary["left_pending"] = len(todo) - start
                _logger.warning("rate limited (retry after %s s); %d document(s) left pending",
                                err.retry_after, summary["left_pending"])
                break
            except Exception:
                summary["chunk_errors"] += 1
                if not last_attempt:
                    raise
                _logger.exception("location inference failed for %s; marking them failed", chunk)
                summary["failed"] += mark_failed(chunk)
                continue
            write_results({d: results[d] for d in chunk if d in results}, summary)
            # a document the finder left out of its reply is not retried forever
            missing = [d for d in chunk if d not in results]
            if missing:
                summary["failed"] += mark_failed(missing)
    finally:
        records.close()
    summary["title_untranslated"] = getattr(translator, "disabled", 0)
    return summary
