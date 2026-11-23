"""The web app's background jobs as Airflow DAG runs (dags/yaml/app.yml).

Runtime(jobs=AirflowJobs()) hands page refreshes and translations to Airflow;
the /database_update status and the /translate progress are read back from
the DAG runs. A job's name is its Airflow run id."""

from birddog.airflow_client import (
    ACTIVE_STATES,
    AirflowError,
    AirflowNotFound,
    get_client,
    run_progress,
)
from birddog.runtime import BackgroundJobs
from birddog.wiki import canonicalize_title
from birddog.log import get_logger

_logger = get_logger()

REFRESH_DAG = "app_refresh_page"
TRANSLATE_DAG = "app_translate_page"
WATCHLIST_DAG = "app_watchlist_check"


class AirflowJobs(BackgroundJobs):
    def __init__(self, client=None):
        self._client = client or get_client()

    @property
    def available(self):
        return self._client.configured

    @property
    def client(self):
        return self._client

    # --- starting jobs -------------------------------------------------------

    def _active_runs(self, dag_id):
        return self._client.list_dag_runs(dag_id, states=ACTIVE_STATES, limit=100)

    def _trigger_once(self, dag_id, conf):
        """Triggers dag_id unless a run with the same conf is already queued or
        running (a double click, or two users on the same page). Returns the run."""
        for run in self._active_runs(dag_id):
            if (run.get("conf") or {}) == conf:
                return run
        return self._client.trigger_dag(dag_id, conf=conf)

    def request_page_refresh(self, titles, deep=False, update_locations=False):
        if isinstance(titles, str):
            titles = [titles]
        return [self._trigger_once(REFRESH_DAG, {
                    "title": canonicalize_title(title),
                    "deep": bool(deep),
                    "update_locations": bool(update_locations)})
                for title in titles]

    def request_translation(self, title):
        return self._trigger_once(TRANSLATE_DAG, {"title": canonicalize_title(title)})

    def request_watch_check(self, email, archive_title):
        """Starts a check of one watchlist entry; returns the run."""
        return self._trigger_once(WATCHLIST_DAG, {"email": email, "archive_title": archive_title})

    def watch_check_state(self, run_id):
        """State of a watch check run (queued/running/success/failed)."""
        return self._client.get_dag_run(WATCHLIST_DAG, run_id)["state"]

    # --- status --------------------------------------------------------------

    def _progress(self, dag_id, run):
        try:
            return run_progress(self._client.get_task_instances(dag_id, run["dag_run_id"]))
        except AirflowError:
            return 0, 0

    def status(self):
        """Active refreshes, as the /database_update 'tasks' dict."""
        try:
            runs = self._active_runs(REFRESH_DAG)
        except AirflowError as err:
            _logger.warning(f"AirflowJobs.status: {err}")
            return {}
        tasks = {}
        for run in runs:
            conf = run.get("conf") or {}
            completed, total = self._progress(REFRESH_DAG, run)
            tasks[run["dag_run_id"]] = {
                "kind": "refresh",
                "description": conf.get("title", ""),
                "deep": bool(conf.get("deep")),
                "update_locations": bool(conf.get("update_locations")),
                "state": run.get("state"),
                "started": run.get("start_date") or run.get("queued_at"),
                "total": total,
                "completed": completed,
            }
        return tasks

    def active_translations(self):
        """Active page translations, as [{name, completed, length}]."""
        try:
            runs = self._active_runs(TRANSLATE_DAG)
        except AirflowError as err:
            _logger.warning(f"AirflowJobs.active_translations: {err}")
            return []
        result = []
        for run in runs:
            completed, total = self._progress(TRANSLATE_DAG, run)
            result.append({"name": (run.get("conf") or {}).get("title", ""),
                           "completed": completed, "length": max(total, 1)})
        return result

    def cancel(self, task_name):
        """Cancels a refresh run (marks it failed, which stops its tasks)."""
        try:
            run = self._client.get_dag_run(REFRESH_DAG, task_name)
        except AirflowNotFound:
            raise KeyError(task_name)
        if run.get("state") in ACTIVE_STATES:
            self._client.set_dag_run_state(REFRESH_DAG, task_name, "failed")
