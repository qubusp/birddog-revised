"""Minimal client for the Airflow 3 REST API (/api/v2), used by the web app to
start background work (DAG runs) and report on it.

Authentication: a JWT from the FAB auth manager's /auth/token endpoint, for a
dedicated Airflow service user. The token is cached and fetched again when the
API answers 401 (expired).

Configuration (environment):
    AIRFLOW_API_URL       e.g. http://airflow-apiserver:8080 (no /api/v2)
    AIRFLOW_API_USER      service user name
    AIRFLOW_API_PASSWORD  its password
"""

import os
import threading

import requests

# DAG run states, as Airflow reports them
ACTIVE_STATES = ("queued", "running")
FINISHED_STATES = ("success", "failed")

_DEFAULT_TIMEOUT = 10  # seconds per request


class AirflowError(Exception):
    """The Airflow API is unreachable, misconfigured or refused a request."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class AirflowNotFound(AirflowError):
    pass


class AirflowClient:
    def __init__(self, base_url=None, username=None, password=None,
                 timeout=_DEFAULT_TIMEOUT, session=None):
        self._base_url = (base_url or "").rstrip("/")
        self._username = username
        self._password = password
        self._timeout = timeout
        self._session = session or requests.Session()
        self._token = None
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls):
        return cls(base_url=os.environ.get("AIRFLOW_API_URL"),
                   username=os.environ.get("AIRFLOW_API_USER"),
                   password=os.environ.get("AIRFLOW_API_PASSWORD"))

    @property
    def configured(self):
        return bool(self._base_url and self._username and self._password)

    # --- transport -----------------------------------------------------------

    def _fetch_token(self):
        try:
            resp = self._session.post(
                f"{self._base_url}/auth/token",
                json={"username": self._username, "password": self._password},
                timeout=self._timeout)
        except requests.RequestException as err:
            raise AirflowError(f"Airflow unreachable: {err}") from err
        if resp.status_code not in (200, 201):
            raise AirflowError(f"Airflow login failed (HTTP {resp.status_code})", resp.status_code)
        token = resp.json().get("access_token")
        if not token:
            raise AirflowError("Airflow login returned no access_token")
        return token

    def _auth_header(self, refresh=False):
        with self._lock:
            if refresh or not self._token:
                self._token = self._fetch_token()
            return {"Authorization": f"Bearer {self._token}"}

    def _request(self, method, path, params=None, json=None):
        if not self.configured:
            raise AirflowError("Airflow API is not configured (AIRFLOW_API_URL/USER/PASSWORD)")
        url = f"{self._base_url}/api/v2{path}"
        for attempt in (0, 1):
            try:
                resp = self._session.request(
                    method, url, params=params, json=json,
                    headers=self._auth_header(refresh=attempt > 0), timeout=self._timeout)
            except requests.RequestException as err:
                raise AirflowError(f"Airflow unreachable: {err}") from err
            # an expired token: log in again once
            if resp.status_code != 401:
                break
        if resp.status_code == 404:
            raise AirflowNotFound(f"{method} {path}: not found", 404)
        if resp.status_code >= 400:
            raise AirflowError(f"{method} {path}: HTTP {resp.status_code}: {resp.text[:300]}",
                               resp.status_code)
        return resp.json() if resp.content else None

    # --- DAG runs ------------------------------------------------------------

    def trigger_dag(self, dag_id, conf=None, run_id=None, note=None):
        """Starts a DAG run now; returns the run (dict with dag_run_id, state, ...)."""
        body = {"logical_date": None, "conf": conf or {}}
        if run_id:
            body["dag_run_id"] = run_id
        if note:
            body["note"] = note
        return self._request("POST", f"/dags/{dag_id}/dagRuns", json=body)

    def get_dag_run(self, dag_id, run_id):
        return self._request("GET", f"/dags/{dag_id}/dagRuns/{run_id}")

    def list_dag_runs(self, dag_id, states=None, limit=25, conf_contains=None):
        """Most recent runs first."""
        params = {"limit": limit, "order_by": "-run_after"}
        if states:
            params["state"] = list(states)
        if conf_contains:
            params["conf_contains"] = conf_contains
        return self._request("GET", f"/dags/{dag_id}/dagRuns", params=params)["dag_runs"]

    def get_task_instances(self, dag_id, run_id):
        return self._request(
            "GET", f"/dags/{dag_id}/dagRuns/{run_id}/taskInstances",
            params={"limit": 1000})["task_instances"]

    def set_dag_run_state(self, dag_id, run_id, state):
        """state: queued / success / failed. Marking a running run failed is how
        it is cancelled: Airflow stops its running tasks."""
        return self._request("PATCH", f"/dags/{dag_id}/dagRuns/{run_id}", json={"state": state})


def run_progress(task_instances):
    """(completed, total) task instances of a run; mapped tasks count once per
    mapped instance. Skipped/failed tasks count as completed."""
    done = {"success", "failed", "skipped", "upstream_failed", "removed"}
    total = len(task_instances)
    completed = sum(1 for ti in task_instances if ti.get("state") in done)
    return completed, total


_client = None


def get_client():
    """The process-wide client, configured from the environment."""
    global _client
    if _client is None:
        _client = AirflowClient.from_env()
    return _client
