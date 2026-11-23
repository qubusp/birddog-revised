"""One-off import of a NocoDB base (Pages, Documents, FTP Site, Locations and
their links) into Postgres.

Two steps, both safe to re-run:
  land(table)   pulls every record through the v3 API into raw.nocodb_record,
                unchanged (fields as JSON)
  transform()   upserts raw.nocodb_record into the core tables with SQL

Configuration (environment):
  NOCODB_IMPORT_TOKEN   API token (xc-token)
  NOCODB_IMPORT_BASE    base id (default: the STAGE base)
  NOCODB_IMPORT_HOST    default https://app.nocodb.com

NocoDB's inline link lists stop at 1,000 entries, so page parents come from
each page's single `parent` link (never from a parent's `children`), and the
document link lists are checked against NocoDB's own count rollups."""

import json
import logging
import os
import time

import requests
from psycopg2.extras import execute_values

from birddog import pg

_logger = logging.getLogger(__name__)

_DEFAULT_BASE = "phb0w28bqvl85ki"   # CLOUD_STAGE in resources/db_config.json
_PAGE_SIZE = 100                    # the v3 API's maximum
# NocoDB Cloud answers bursts with 429 + Retry-After (~30 s). Requests go out
# one at a time at a steady pace, and each 429 slows the pace down for good.
_MIN_INTERVAL = 1.0                 # seconds between requests, to start with
_MAX_INTERVAL = 10.0

TABLES = {
    "pages": "Pages",
    "documents": "Documents",
    "ftp": "FTP Site",
    "locations": "Locations",
}


def _config():
    return (os.environ.get("NOCODB_IMPORT_HOST", "https://app.nocodb.com"),
            os.environ.get("NOCODB_IMPORT_BASE", _DEFAULT_BASE),
            os.environ["NOCODB_IMPORT_TOKEN"])


class _Pacer:
    """Spaces requests at least `interval` seconds apart; slow_down() is
    called on a 429 and makes the spacing permanently longer."""

    def __init__(self, interval=_MIN_INTERVAL):
        self.interval = interval
        self._last = 0.0

    def wait(self):
        delay = self._last + self.interval - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        self._last = time.monotonic()

    def slow_down(self):
        self.interval = min(self.interval * 1.5, _MAX_INTERVAL)


def _get(session, url, params=None, pacer=None):
    # NocoDB Cloud's rate limit can hold for minutes: honour Retry-After and
    # keep backing off (up to ~15 minutes in total) instead of failing the task
    for attempt in range(20):
        if pacer:
            pacer.wait()
        resp = session.get(url, params=params, timeout=120)
        if resp.status_code == 429 or resp.status_code >= 500:
            if pacer and resp.status_code == 429:
                pacer.slow_down()
            retry_after = resp.headers.get("Retry-After", "")
            wait = int(retry_after) if retry_after.isdigit() else min(2 ** attempt, 60)
            _logger.info(f"nocodb_import: {resp.status_code}, retrying in {wait}s")
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp.json()
    resp.raise_for_status()


def _table_ids(session, host, base):
    tables = _get(session, f"{host}/api/v2/meta/bases/{base}/tables")["list"]
    return {t["title"]: t["id"] for t in tables}


def land(table_key, full=False):
    """Pulls one NocoDB table into raw.nocodb_record, paging by record id
    (Id > last landed id) so each request is cheap and an interrupted run
    resumes where it stopped. full=True starts from the first record again.
    Returns the expected and landed counts."""
    host, base, token = _config()
    session = requests.Session()
    session.headers["xc-token"] = token
    pacer = _Pacer()
    table_id = _table_ids(session, host, base)[TABLES[table_key]]
    total = _get(session, f"{host}/api/v2/tables/{table_id}/records/count", pacer=pacer)["count"]
    url = f"{host}/api/v3/data/{base}/{table_id}/records"

    with pg.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT max(nocodb_id) FROM raw.nocodb_record WHERE table_name = %s",
                    (table_key,))
        last_id = 0 if full else (cur.fetchone()[0] or 0)
    _logger.info(f"nocodb_import: {table_key}: {total} records, starting after id {last_id}")

    while True:
        data = _get(session, url, {"where": f"(Id,gt,{last_id})", "pageSize": _PAGE_SIZE},
                    pacer=pacer)
        records = data.get("records", [])
        if not records:
            break
        rows = [(table_key, r["id"], json.dumps(r["fields"], ensure_ascii=False))
                for r in records]
        with pg.connect() as conn, conn.cursor() as cur:
            execute_values(cur, """
                INSERT INTO raw.nocodb_record (table_name, nocodb_id, fields) VALUES %s
                ON CONFLICT (table_name, nocodb_id)
                DO UPDATE SET fields = EXCLUDED.fields, landed_at = now()""", rows)
        new_last = max(r["id"] for r in records)
        if new_last <= last_id:
            raise RuntimeError(f"nocodb_import: {table_key}: paging stalled at id {last_id}")
        last_id = new_last
        if len(records) < _PAGE_SIZE:
            break

    with pg.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM raw.nocodb_record WHERE table_name = %s", (table_key,))
        landed = cur.fetchone()[0]
    _logger.info(f"nocodb_import: {table_key}: {landed}/{total} landed "
                 f"(request interval {pacer.interval:.1f}s)")
    return {"table": table_key, "expected": total, "landed": landed}


_TRANSFORM_SQL = os.path.join(os.path.dirname(__file__), "nocodb_import.sql")


def transform():
    """Upserts the landed records into core.*. Returns per-table counts and
    the number of link lists that looked truncated."""
    with open(_TRANSFORM_SQL, encoding="utf8") as f:
        sql = f.read()
    with pg.connect() as conn, conn.cursor() as cur:
        cur.execute(sql)
        cur.execute("""
            SELECT 'pages', count(*) FROM core.page UNION ALL
            SELECT 'fresh_pages', count(*) FROM core.page WHERE NOT stale UNION ALL
            SELECT 'documents', count(*) FROM core.document UNION ALL
            SELECT 'page_document', count(*) FROM core.page_document UNION ALL
            SELECT 'document_duplicate', count(*) FROM core.document_duplicate UNION ALL
            SELECT 'location', count(*) FROM core.location UNION ALL
            SELECT 'document_location', count(*) FROM core.document_location UNION ALL
            SELECT 'ftp_entry', count(*) FROM core.ftp_entry UNION ALL
            SELECT 'ftp_document', count(*) FROM core.ftp_document UNION ALL
            SELECT 'translation', count(*) FROM core.translation""")
        counts = dict(cur.fetchall())
        # inline link lists shorter than NocoDB's own count were cut off
        cur.execute("""
            SELECT count(*) FILTER (WHERE jsonb_array_length(COALESCE(fields->'owning_pages', '[]')) < COALESCE((fields->>'num_owners')::int, 0)),
                   count(*) FILTER (WHERE jsonb_array_length(COALESCE(fields->'duplicates', '[]')) < COALESCE((fields->>'num_duplicates')::int, 0)),
                   count(*) FILTER (WHERE jsonb_array_length(COALESCE(fields->'locations', '[]')) < COALESCE((fields->>'num_locations')::int, 0))
            FROM raw.nocodb_record WHERE table_name = 'documents'""")
        owners, dupes, locs = cur.fetchone()
    counts["truncated_links"] = {"owning_pages": owners, "duplicates": dupes, "locations": locs}
    return counts
