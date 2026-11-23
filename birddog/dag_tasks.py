"""Task callables for the YAML DAGs in dags/yaml/ (dag-factory).

Each function is one Airflow task: it takes plain JSON-able arguments (or the
output of an upstream task) and returns a small summary. The work itself lives
in birddog.pipeline / birddog.nocodb_import; this module only shapes it into
batches for dynamic task mapping."""

# pages fetched per DAG run, split into batches that run in parallel
# (bounded by the wiki_api pool)
PAGES_PER_RUN = 400
PAGE_BATCH = 20
DOCS_PER_RUN = 1000
DOC_BATCH = 50
# texts per Google Translate request (the old TranslationManager used 50)
TRANSLATE_PER_RUN = 5000
TRANSLATE_BATCH = 50


def _chunks(items, size):
    return [items[i:i + size] for i in range(0, len(items), size)]


# --- wiki --------------------------------------------------------------------

def ingest_recent_changes():
    from birddog.pipeline import ingest_recent_changes as run
    return run()


def stale_page_batches():
    from birddog.pipeline import stale_page_titles
    return _chunks(stale_page_titles(PAGES_PER_RUN), PAGE_BATCH)


def refresh_page_batch(titles):
    from birddog.pipeline import refresh_pages
    return refresh_pages(titles)


def doc_metadata_batches():
    from birddog.pipeline import docs_missing_metadata
    return _chunks(docs_missing_metadata(DOCS_PER_RUN), DOC_BATCH)


def refresh_doc_metadata_batch(docs):
    from birddog.pipeline import refresh_doc_metadata
    return refresh_doc_metadata(docs)


# --- enrichment --------------------------------------------------------------

def untranslated_batches():
    from birddog.pipeline import untranslated_pages
    return _chunks(untranslated_pages(TRANSLATE_PER_RUN), TRANSLATE_BATCH)


def translate_batch(pages):
    from birddog.pipeline import translate_pages
    return translate_pages(pages)


# --- ops ---------------------------------------------------------------------

def check_birddog_library():
    from birddog.wiki import canonicalize_title
    return canonicalize_title("Архів:ДАЗО/1")


def check_birddog_database():
    from birddog import pg
    with pg.connect() as conn, conn.cursor() as cur:
        cur.execute("""SELECT schema_name FROM information_schema.schemata
                       WHERE schema_name IN ('raw', 'core', 'app') ORDER BY 1""")
        schemas = [r[0] for r in cur.fetchall()]
    assert schemas == ["app", "core", "raw"], schemas
    return schemas


def compare_restored_counts():
    """Fails when a table has fewer rows in the restored copy than allowed for
    a week's growth, or is missing from it."""
    import os
    import psycopg2

    def counts(dbname):
        conn = psycopg2.connect(host="postgres", user="postgres",
                                password=os.environ["POSTGRES_PASSWORD"], dbname=dbname)
        with conn, conn.cursor() as cur:
            cur.execute("""SELECT schemaname || '.' || relname FROM pg_stat_user_tables
                           WHERE schemaname IN ('raw', 'core', 'app')""")
            result = {}
            for (table,) in cur.fetchall():
                cur.execute(f"SELECT count(*) FROM {table}")
                result[table] = cur.fetchone()[0]
        conn.close()
        return result

    live, restored = counts("birddog"), counts("birddog_restore_check")
    problems = [f"{t}: live {n}, restored {restored.get(t)}"
                for t, n in live.items()
                # the backup is up to a week old, so allow it to be behind by 10%
                if restored.get(t) is None or restored[t] < n * 0.9]
    if problems:
        raise ValueError("restore check failed: " + "; ".join(problems))
    return {"tables": len(live)}


# --- migration ---------------------------------------------------------------

NOCODB_TABLES = ["pages", "documents", "ftp", "locations"]


def land_nocodb_table(table):
    from birddog.nocodb_import import land
    result = land(table)
    if result["landed"] < result["expected"]:
        raise ValueError(f"landed fewer records than NocoDB reports: {result}")
    return result


def transform_nocodb():
    from birddog.nocodb_import import transform
    return transform()


# --- dump backfill -----------------------------------------------------------

def load_wiki_dump(dump_date=None):
    from birddog.dump_backfill import load
    return load(dump_date or None)


def verify_wiki_dump(dump_date=None):
    """Fails the DAG run if the dump-derived pages don't match a live sample."""
    from birddog.dump_backfill import verify_sample
    result = verify_sample(dump_date or None, size=300)
    worst = min(result["match_percent"].values())
    if worst < 98:
        raise ValueError(f"dump doesn't match the live wiki well enough: {result}")
    return result
