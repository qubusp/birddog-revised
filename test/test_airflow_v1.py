from unittest.mock import MagicMock, patch

import pytest

from birddog import airflow_v1
from birddog.service import app

INTERNAL = "http://birddog-web:8000"
AUTH = {"Authorization": "Basic bWFybW90OnNlY3JldA=="}   # marmot:secret


def _resp(status, body):
    r = MagicMock(status_code=status, text="")
    r.json.return_value = body
    return r


@pytest.fixture
def v2(monkeypatch):
    """Fake Airflow: /auth/token plus a dict of /api/v2 paths -> JSON."""
    monkeypatch.setenv("AIRFLOW_API_URL", "http://airflow:8080")
    airflow_v1._tokens.clear()
    routes = {}
    def get(url, params=None, headers=None, timeout=None):
        path = url.split("/api/v2", 1)[1]
        get.calls.append((path, params))
        return _resp(200, routes[path])
    get.calls = []
    with patch("birddog.airflow_v1.requests.post", return_value=_resp(201, {"access_token": "jwt"})), \
         patch("birddog.airflow_v1.requests.get", side_effect=get):
        yield routes, get


def test_public_host_and_missing_auth_are_refused(v2):
    c = app.test_client()
    assert c.get("/airflow-v1/api/v1/dags", base_url="https://birddog.example.org", headers=AUTH).status_code == 404
    assert c.get("/airflow-v1/api/v1/dags", base_url=INTERNAL).status_code == 401


def test_dags_get_v1_shapes(v2):
    routes, get = v2
    routes["/dags"] = {"total_entries": 1, "dags": [{
        "dag_id": "refresh_pages", "is_stale": False, "is_paused": False, "owners": ["airflow"],
        "tags": [{"name": "wiki", "dag_id": "refresh_pages"}], "timetable_summary": "0 3 * * *",
        "description": None, "fileloc": "/opt/airflow/dags/x.py", "max_active_runs": 1}]}
    body = app.test_client().get("/airflow-v1/api/v1/dags?only_active=true&limit=100&offset=0",
                                 base_url=INTERNAL, headers=AUTH).json
    dag = body["dags"][0]
    assert dag["is_active"] is True and dag["tags"] == [{"name": "wiki"}]
    assert dag["schedule_interval"] == {"__type": "CronExpression", "value": "0 3 * * *"}
    assert get.calls[0][1]["exclude_stale"] == "true"


def test_tasks_flatten_v2_objects_and_add_upstream(v2):
    routes, _ = v2
    routes["/dags/d/tasks"] = {"tasks": [
        {"task_id": "a", "downstream_task_ids": ["b"], "class_ref": {"module_path": "m", "class_name": "Op"},
         "execution_timeout": {"__type": "TimeDelta", "days": 0, "seconds": 60}, "extra_links": ["x"]},
        {"task_id": "b", "downstream_task_ids": []}]}
    tasks = app.test_client().get("/airflow-v1/api/v1/dags/d/tasks", base_url=INTERNAL, headers=AUTH).json["tasks"]
    assert tasks[0]["class_ref"] == "m.Op" and tasks[0]["execution_timeout"] is None
    assert tasks[0]["extra_links"] == [] and tasks[1]["upstream_task_ids"] == ["a"]


def test_runs_map_execution_date(v2):
    routes, get = v2
    routes["/dags/d/dagRuns"] = {"total_entries": 1, "dag_runs": [
        {"dag_run_id": "manual__1", "dag_id": "d", "logical_date": None, "run_after": "2026-11-24T00:00:00Z",
         "run_type": "manual", "state": "success", "conf": None}]}
    runs = app.test_client().get("/airflow-v1/api/v1/dags/d/dagRuns?order_by=-execution_date"
                                 "&execution_date_gte=2026-11-01T00:00:00Z", base_url=INTERNAL, headers=AUTH).json
    assert runs["dag_runs"][0]["execution_date"] == "2026-11-24T00:00:00Z"
    assert runs["dag_runs"][0]["external_trigger"] is True and runs["dag_runs"][0]["conf"] == {}
    assert get.calls[0][1] == {"limit": "100", "order_by": "-run_after", "run_after_gte": "2026-11-01T00:00:00Z"} \
        or get.calls[0][1]["order_by"] == "-run_after"


def test_assets_become_datasets_with_lineage(v2):
    routes, _ = v2
    routes["/assets"] = {"total_entries": 1, "assets": [{
        "id": 1, "uri": "postgres://postgres:5432/birddog/core/document", "extra": {},
        "producing_tasks": [{"dag_id": "refresh_pages", "task_id": "refresh"}],
        "scheduled_dags": [{"dag_id": "refresh_doc_metadata"}]}]}
    ds = app.test_client().get("/airflow-v1/api/v1/datasets", base_url=INTERNAL, headers=AUTH).json["datasets"][0]
    assert ds["producing_tasks"] == [{"dag_id": "refresh_pages", "task_id": "refresh"}]
    assert ds["consuming_dags"] == [{"dag_id": "refresh_doc_metadata"}]
