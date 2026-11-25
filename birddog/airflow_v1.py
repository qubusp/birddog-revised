"""Airflow 2 REST API (/api/v1) answered from Airflow 3's /api/v2, for Marmot.

Marmot's Airflow connector (marmot 0.14, plugins/airflow) only speaks the
Airflow 2 API, which Airflow 3 removed. This blueprint answers the six read
calls that connector makes, from /api/v2, renaming what changed: datasets are
assets, execution_date is logical_date, is_active is (not) is_stale. Marmot
then catalogs DAGs, tasks and run history, and links DAGs to the Postgres
tables they produce and consume (Airflow assets such as
postgres://postgres:5432/birddog/core/document).

Marmot's pipeline points at http://birddog-web:8000/airflow-v1 with basic auth
(an Airflow user and password); each request trades those for a JWT at
Airflow's /auth/token, cached briefly. Read-only, GET only.

Gotchas:
- Only answered on the internal host name (birddog-web:8000), never through
  Caddy: the public birddog.<domain>/airflow-v1 returns 404.
- The connector is written in Go and rejects a whole response when one field
  has the wrong JSON type, so every object below is rebuilt with the v1
  field types rather than passed through (e.g. class_ref and
  execution_timeout are strings in v1, objects in v2).
"""

import os
import threading
import time

import requests
from flask import Blueprint, jsonify, request

bp = Blueprint("airflow_v1", __name__, url_prefix="/airflow-v1/api/v1")

_TIMEOUT = 30
_tokens = {}                 # (user, password) -> (token, expires_at)
_tokens_lock = threading.Lock()


def _base():
    return (os.environ.get("AIRFLOW_API_URL") or "").rstrip("/")


@bp.before_request
def _internal_only():
    host = (request.host or "").split(":")[0]
    if host != "birddog-web":
        return jsonify(error="Not found"), 404
    if not request.authorization or not request.authorization.username:
        return jsonify(detail="Basic authentication required"), 401
    return None


def _token():
    auth = request.authorization
    key = (auth.username, auth.password or "")
    with _tokens_lock:
        cached = _tokens.get(key)
        if cached and cached[1] > time.time():
            return cached[0]
    resp = requests.post(f"{_base()}/auth/token", json={"username": key[0], "password": key[1]},
                         timeout=_TIMEOUT)
    if resp.status_code not in (200, 201):
        return None
    token = resp.json()["access_token"]
    with _tokens_lock:
        _tokens[key] = (token, time.time() + 600)
    return token


def _get(path, params=None):
    """GET /api/v2<path>; returns (json, error_response)."""
    token = _token()
    if token is None:
        return None, (jsonify(detail="Airflow login failed"), 401)
    resp = requests.get(f"{_base()}/api/v2{path}", params=params,
                        headers={"Authorization": f"Bearer {token}"}, timeout=_TIMEOUT)
    if resp.status_code >= 400:
        return None, (jsonify(detail=resp.text[:300]), resp.status_code)
    return resp.json(), None


def _str(value):
    return value if isinstance(value, str) else None


# --- DAGs ----------------------------------------------------------------------

def _dag(d):
    summary = d.get("timetable_summary")
    return {
        "dag_id": d["dag_id"],
        "description": _str(d.get("description")),
        "file_token": d.get("file_token") or "",
        "fileloc": d.get("fileloc") or "",
        "is_paused": bool(d.get("is_paused")),
        "is_active": not d.get("is_stale", False),
        "is_subdag": False,
        "last_parsed_time": _str(d.get("last_parsed_time")),
        "last_pickled": None,
        "last_expired": _str(d.get("last_expired")),
        "scheduler_lock": None,
        "pickle_id": None,
        "default_view": "grid",
        "owners": [o for o in d.get("owners") or [] if isinstance(o, str)],
        "tags": [{"name": t["name"]} for t in d.get("tags") or [] if isinstance(t, dict) and t.get("name")],
        "schedule_interval": ({"__type": "CronExpression", "value": summary} if isinstance(summary, str) else None),
        "timetable_description": _str(d.get("timetable_description")),
        "next_dagrun": _str(d.get("next_dagrun_logical_date") or d.get("next_dagrun_run_after")),
        "next_dagrun_data_interval_start": _str(d.get("next_dagrun_data_interval_start")),
        "max_active_runs": d.get("max_active_runs") or 0,
        "max_active_tasks": d.get("max_active_tasks") or 0,
        "has_task_concurrency_limits": bool(d.get("has_task_concurrency_limits")),
        "has_import_errors": bool(d.get("has_import_errors")),
    }


@bp.get("/dags")
def list_dags():
    params = {"limit": request.args.get("limit", 100), "offset": request.args.get("offset", 0)}
    if request.args.get("only_active") == "true":
        params["exclude_stale"] = "true"
    data, error = _get("/dags", params)
    if error:
        return error
    return jsonify(dags=[_dag(d) for d in data.get("dags", [])], total_entries=data.get("total_entries", 0))


@bp.get("/dags/<dag_id>")
def get_dag(dag_id):
    data, error = _get(f"/dags/{dag_id}")
    return error or jsonify(_dag(data))


# --- tasks ---------------------------------------------------------------------

def _timedelta(value):
    if isinstance(value, dict) and "days" in value:
        return {"__type": "TimeDelta", "days": int(value.get("days") or 0),
                "seconds": int(value.get("seconds") or 0), "microseconds": int(value.get("microseconds") or 0)}
    return None


def _class_ref(value):
    if isinstance(value, dict):
        return ".".join(p for p in (value.get("module_path"), value.get("class_name")) if p)
    return value if isinstance(value, str) else ""


def _task(t, upstream):
    return {
        "task_id": t["task_id"],
        "task_display_name": t.get("task_display_name") or t["task_id"],
        "operator_name": t.get("operator_name") or "",
        "class_ref": _class_ref(t.get("class_ref")),
        "pool": t.get("pool") or "",
        "pool_slots": int(t.get("pool_slots") or 1),
        "execution_timeout": None,
        "trigger_rule": t.get("trigger_rule") or "",
        "retries": int(t.get("retries") or 0),
        "retry_delay": _timedelta(t.get("retry_delay")),
        "retry_exponential_backoff": bool(t.get("retry_exponential_backoff")),
        "priority_weight": int(t.get("priority_weight") or 0),
        "weight_rule": t.get("weight_rule") or "",
        "queue": t.get("queue") or "",
        "downstream_task_ids": list(t.get("downstream_task_ids") or []),
        "upstream_task_ids": upstream.get(t["task_id"], []),
        "depends_on_past": bool(t.get("depends_on_past")),
        "wait_for_downstream": bool(t.get("wait_for_downstream")),
        "start_date": _str(t.get("start_date")),
        "end_date": _str(t.get("end_date")),
        "ui_color": t.get("ui_color") or "",
        "ui_fgcolor": t.get("ui_fgcolor") or "",
        "template_fields": [f for f in t.get("template_fields") or [] if isinstance(f, str)],
        "extra_links": [],
        "params": {},
    }


@bp.get("/dags/<dag_id>/tasks")
def list_tasks(dag_id):
    data, error = _get(f"/dags/{dag_id}/tasks")
    if error:
        return error
    tasks = data.get("tasks", [])
    upstream = {}
    for t in tasks:
        for down in t.get("downstream_task_ids") or []:
            upstream.setdefault(down, []).append(t["task_id"])
    return jsonify(tasks=[_task(t, upstream) for t in tasks], total_entries=len(tasks))


# --- DAG runs ------------------------------------------------------------------

def _run(r):
    logical = r.get("logical_date") or r.get("run_after") or ""
    conf = r.get("conf")
    return {
        "dag_run_id": r.get("dag_run_id") or "",
        "dag_id": r.get("dag_id") or "",
        "logical_date": logical,
        "execution_date": logical,
        "start_date": _str(r.get("start_date")),
        "end_date": _str(r.get("end_date")),
        "data_interval_start": _str(r.get("data_interval_start")),
        "data_interval_end": _str(r.get("data_interval_end")),
        "last_scheduling_decision": _str(r.get("last_scheduling_decision")),
        "run_type": r.get("run_type") or "",
        "state": r.get("state") or "",
        "external_trigger": r.get("run_type") == "manual",
        "conf": conf if isinstance(conf, dict) else {},
        "note": _str(r.get("note")),
    }


@bp.get("/dags/<dag_id>/dagRuns")
def list_runs(dag_id):
    params = {"limit": request.args.get("limit", 100)}
    order = request.args.get("order_by")
    if order:
        params["order_by"] = order.replace("execution_date", "run_after")
    if request.args.get("execution_date_gte"):
        params["run_after_gte"] = request.args["execution_date_gte"]
    data, error = _get(f"/dags/{dag_id}/dagRuns", params)
    if error:
        return error
    return jsonify(dag_runs=[_run(r) for r in data.get("dag_runs", [])],
                   total_entries=data.get("total_entries", 0))


# --- datasets (assets) ---------------------------------------------------------

def _dataset(a):
    extra = a.get("extra")
    return {
        "id": int(a.get("id") or 0),
        "uri": a.get("uri") or "",
        "extra": extra if isinstance(extra, dict) else {},
        "created_at": a.get("created_at") or "",
        "updated_at": a.get("updated_at") or "",
        "consuming_dags": [{"dag_id": d["dag_id"]} for d in a.get("scheduled_dags") or [] if d.get("dag_id")],
        "producing_tasks": [{"dag_id": t["dag_id"], "task_id": t["task_id"]}
                            for t in a.get("producing_tasks") or [] if t.get("dag_id") and t.get("task_id")],
    }


@bp.get("/datasets")
def list_datasets():
    data, error = _get("/assets", {"limit": request.args.get("limit", 100),
                                    "offset": request.args.get("offset", 0)})
    if error:
        return error
    return jsonify(datasets=[_dataset(a) for a in data.get("assets", [])],
                   total_entries=data.get("total_entries", 0))


@bp.get("/datasetEvents")
def list_dataset_events():
    params = {"limit": request.args.get("limit", 100)}
    if request.args.get("order_by"):
        params["order_by"] = request.args["order_by"]
    uri = request.args.get("dataset_uri")
    if uri:
        assets, error = _get("/assets", {"uri_pattern": uri, "limit": 100})
        if error:
            return error
        match = next((a for a in assets.get("assets", []) if a.get("uri") == uri), None)
        if match is None:
            return jsonify(dataset_events=[], total_entries=0)
        params["asset_id"] = match["id"]
    data, error = _get("/assets/events", params)
    if error:
        return error
    events = [{
        "id": int(e.get("id") or 0),
        "dataset_id": int(e.get("asset_id") or 0),
        "dataset_uri": e.get("uri") or "",
        "source_dag_id": _str(e.get("source_dag_id")),
        "source_task_id": _str(e.get("source_task_id")),
        "source_run_id": _str(e.get("source_run_id")),
        "source_map_index": int(e.get("source_map_index") if isinstance(e.get("source_map_index"), int) else -1),
        "created_dagruns": [{"dag_id": d["dag_id"]} for d in e.get("created_dagruns") or [] if d.get("dag_id")],
        "timestamp": e.get("timestamp") or "",
        "extra": e.get("extra") if isinstance(e.get("extra"), dict) else {},
    } for e in data.get("asset_events", [])]
    return jsonify(dataset_events=events, total_entries=data.get("total_entries", 0))
