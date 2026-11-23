"""Turns every dags/sources/<name>/source.yml into an Airflow DAG,
source_<name>:

    land -> transform -> verify -> expose

land runs in the source's pool with its retries; verify's outlets are the
assets in `produces`, so downstream DAGs can schedule on them and only run
after the new data passed its checks. A source.yml that doesn't validate
still gets a DAG (tagged `invalid`, never scheduled) whose single task fails
with the validation problems, so the mistake shows up in the Airflow UI
instead of silently dropping the source."""

import logging
from datetime import timedelta
from pathlib import Path

from birddog.sources.spec import SpecError, discover, load_spec

_logger = logging.getLogger(__name__)


# --- task callables (module level, so they import cleanly in the worker) ------

def _run_id():
    try:
        from airflow.sdk import get_current_context
        return get_current_context()["run_id"]
    except Exception:
        return None


def _truthy(value):
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def land_task(source_dir, full_refresh=False):
    from birddog.sources import runner
    return runner.land(source_dir, run_id=_run_id(), full_refresh=_truthy(full_refresh))


def transform_task(source_dir, landed=None):
    from birddog.sources import runner
    return runner.transform(source_dir, (landed or {}).get("batch_id"))


def verify_task(source_dir, transformed=None):
    from birddog.sources import runner
    return runner.verify(source_dir, (transformed or {}).get("batch_id"))


def expose_task(source_dir):
    from birddog.sources import runner
    return runner.expose(source_dir)


def invalid_task(message):
    raise ValueError(message)


# --- DAG construction --------------------------------------------------------

def _schedule(spec):
    from airflow.sdk import Asset
    s = spec.schedule
    if s is None or isinstance(s, str):
        return s
    assets = [Asset(u) for u in s["assets"]]
    if not s["cron"]:
        return assets
    from airflow.timetables.assets import AssetOrTimeSchedule
    from airflow.timetables.trigger import CronTriggerTimetable
    return AssetOrTimeSchedule(timetable=CronTriggerTimetable(s["cron"], timezone="UTC"),
                               assets=assets)


def build_dag(spec):
    import pendulum
    from airflow.sdk import DAG, Asset, Param, task

    doc = [f"**{spec.description}**", "", f"Owner: {spec.owner}",
           f"Source folder: `dags/sources/{spec.name}/` (fetch: `{spec.fetch['type']}`, "
           f"key: `{', '.join(spec.key)}`, mode: `{spec.mode}`)"]
    if spec.notes:
        doc += ["", spec.notes]
    folder = str(spec.folder)
    with DAG(
        dag_id=spec.dag_id,
        description=spec.description[:200],
        doc_md="\n".join(doc),
        schedule=_schedule(spec),
        start_date=pendulum.parse(spec.start_date, tz="UTC"),
        catchup=False,
        max_active_runs=spec.max_active_runs,
        tags=sorted({"source", *spec.tags}),
        default_args={"owner": spec.owner, "retries": spec.retries,
                      "retry_delay": timedelta(seconds=spec.retry_delay_sec)},
        params={"full_refresh": Param(False, type="boolean",
                                      description="Ignore the watermark and fetch everything")},
    ) as dag:
        landed = task(task_id="land", pool=spec.pool)(land_task)(
            folder, full_refresh="{{ params.full_refresh }}")
        transformed = task(task_id="transform")(transform_task)(folder, landed)
        verified = task(task_id="verify", retries=0,
                        outlets=[Asset(u) for u in spec.produces])(verify_task)(folder, transformed)
        if spec.view is not False:
            exposed = task(task_id="expose", outlets=[
                Asset(f"postgres://postgres:5432/birddog/views/{spec.view_name}")])(expose_task)(folder)
            verified >> exposed
    return dag


def build_invalid_dag(name, error):
    import pendulum
    from airflow.sdk import DAG, task
    message = str(error)
    with DAG(dag_id=f"source_{name}", schedule=None, catchup=False,
             start_date=pendulum.datetime(2026, 11, 1, tz="UTC"),
             description="INVALID source.yml - see the task log / doc",
             doc_md="**source.yml does not validate:**\n\n" + "\n".join(
                 f"- {p}" for p in getattr(error, "problems", [message])),
             tags=["source", "invalid"]) as dag:
        task(task_id="invalid_source_yml", retries=0)(invalid_task)(message)
    return dag


def build_dags(sources_dir):
    """{dag_id: DAG} for every source folder; disabled sources are left out."""
    dags = {}
    for folder in discover(Path(sources_dir)):
        try:
            spec = load_spec(folder)
        except SpecError as e:
            _logger.error(f"source {folder.name}: {e}")
            dags[f"source_{folder.name}"] = build_invalid_dag(folder.name, e)
            continue
        if not spec.enabled:
            continue
        dags[spec.dag_id] = build_dag(spec)
    return dags
