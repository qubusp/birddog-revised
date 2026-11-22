"""Wiki pipeline: recent changes -> stale pages -> page refresh -> file metadata.

Nothing here is real time: changes are read once a day. Each DAG reads a
work-queue view and is safe to re-run. Downstream DAGs run when the table they
depend on has been updated (Airflow assets), and hourly as a fallback so a
backlog (e.g. the first full backfill) keeps draining between daily runs.
All wiki API calls share the `wiki_api` pool to stay within MediaWiki's rate
limits."""

from datetime import datetime, timedelta

from airflow.sdk import Asset, dag, task
from airflow.timetables.assets import AssetOrTimeSchedule
from airflow.timetables.trigger import CronTriggerTimetable

PAGES = Asset("postgres://postgres:5432/birddog/core/page")
DOCUMENTS = Asset("postgres://postgres:5432/birddog/core/document")

_START = datetime(2026, 11, 1)
_DEFAULTS = {"retries": 2, "retry_delay": timedelta(minutes=2), "pool": "wiki_api"}

# pages fetched per DAG run, split into batches that run in parallel (bounded by the pool)
_PAGES_PER_RUN = 400
_PAGE_BATCH = 20
_DOCS_PER_RUN = 1000
_DOC_BATCH = 50


def _chunks(items, size):
    return [items[i:i + size] for i in range(0, len(items), size)]


@dag(schedule="0 3 * * *", start_date=_START, catchup=False, max_active_runs=1,
     default_args=_DEFAULTS, tags=["wiki"])
def wiki_changes():

    @task(outlets=[PAGES])
    def ingest():
        from birddog.pipeline import ingest_recent_changes
        return ingest_recent_changes()

    ingest()


@dag(schedule=AssetOrTimeSchedule(timetable=CronTriggerTimetable("15 * * * *", timezone="UTC"),
                                  assets=[PAGES]),
     start_date=_START, catchup=False, max_active_runs=1, default_args=_DEFAULTS, tags=["wiki"])
def refresh_pages():

    @task
    def batches():
        from birddog.pipeline import stale_page_titles
        return _chunks(stale_page_titles(_PAGES_PER_RUN), _PAGE_BATCH)

    @task(outlets=[DOCUMENTS])
    def refresh(titles):
        from birddog.pipeline import refresh_pages as run
        return run(titles)

    refresh.expand(titles=batches())


@dag(schedule=AssetOrTimeSchedule(timetable=CronTriggerTimetable("45 * * * *", timezone="UTC"),
                                  assets=[DOCUMENTS]),
     start_date=_START, catchup=False, max_active_runs=1, default_args=_DEFAULTS, tags=["wiki"])
def refresh_doc_metadata():

    @task
    def batches():
        from birddog.pipeline import docs_missing_metadata
        return _chunks(docs_missing_metadata(_DOCS_PER_RUN), _DOC_BATCH)

    @task
    def fetch(docs):
        from birddog.pipeline import refresh_doc_metadata as run
        return run(docs)

    fetch.expand(docs=batches())


wiki_changes()
refresh_pages()
refresh_doc_metadata()


# texts per Google Translate request (the old TranslationManager used 50)
_TRANSLATE_BATCH = 50
_TRANSLATE_PER_RUN = 5000


@dag(schedule="0 5 * * *", start_date=_START, catchup=False, max_active_runs=1,
     default_args={"retries": 2, "retry_delay": timedelta(minutes=5)}, tags=["enrich"])
def translate():

    @task
    def batches():
        from birddog.pipeline import untranslated_pages
        return _chunks(untranslated_pages(_TRANSLATE_PER_RUN), _TRANSLATE_BATCH)

    @task(max_active_tis_per_dag=2)
    def run(pages):
        from birddog.pipeline import translate_pages
        return translate_pages(pages)

    run.expand(pages=batches())


translate()
