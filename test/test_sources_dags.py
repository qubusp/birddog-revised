"""Source kit: dags/load_sources.py and birddog.sources.dags build the
expected DAGs. Needs Airflow (skipped without it), not a database."""

import shutil
from pathlib import Path

import pytest
import yaml

airflow = pytest.importorskip("airflow")

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _airflow_home(tmp_path, monkeypatch):
    monkeypatch.setenv("AIRFLOW_HOME", str(tmp_path / "airflow_home"))
    monkeypatch.setenv("AIRFLOW__CORE__LOAD_EXAMPLES", "false")


def _dagbag(folder):
    from airflow.dag_processing.dagbag import DagBag
    return DagBag(dag_folder=str(folder))


def test_repo_dags_folder_parses():
    bag = _dagbag(REPO / "dags")
    assert bag.import_errors == {}
    # the existing YAML DAGs are untouched, the worked example is added,
    # the disabled example is not
    assert {"import_nocodb", "wiki_changes", "refresh_pages", "source_nocodb_locations"} <= set(bag.dags)
    assert "source_jewishgen_sftp" not in bag.dags


def test_worked_example_dag_shape():
    dag = _dagbag(REPO / "dags").dags["source_nocodb_locations"]
    tasks = {t.task_id: t for t in dag.tasks}
    assert set(tasks) == {"land", "transform", "verify", "expose"}
    assert tasks["transform"].upstream_task_ids == {"land"}
    assert tasks["verify"].upstream_task_ids == {"transform"}
    assert tasks["expose"].upstream_task_ids == {"verify"}
    assert [a.uri for a in tasks["verify"].outlets] == ["postgres://postgres:5432/birddog/core/location"]
    assert tasks["land"].pool == "default_pool" and tasks["land"].retries == 1
    assert tasks["verify"].retries == 0
    assert dag.max_active_runs == 1 and not dag.catchup
    assert "source" in dag.tags and dag.params["full_refresh"] is False
    assert dag.timetable.__class__.__name__ == "NullTimetable"


def test_dags_serialize():
    from airflow.serialization.serialized_objects import DagSerialization
    for dag in _dagbag(REPO / "dags").dags.values():
        if dag.fileloc.endswith("load_sources.py"):
            DagSerialization.to_dict(dag)


def _source(folder, **overrides):
    spec = {
        "name": folder.name, "description": "Test source", "owner": "tests",
        "fetch": {"type": "csv", "url": "https://example.org/x.csv"}, "key": "id",
        "transform": "transform.sql",
        "verify": [{"check": "row_count", "table": "core.page"}],
        "produces": ["core/page", "core/document"], "pool": "wiki_api",
    }
    spec.update(overrides)
    folder.mkdir(parents=True)
    (folder / "source.yml").write_text(yaml.safe_dump(spec))
    (folder / "transform.sql").write_text("SELECT 1;")


def test_loader_variants(tmp_path):
    dags_dir = tmp_path / "dags"
    (dags_dir / "sources").mkdir(parents=True)
    shutil.copy(REPO / "dags" / "load_sources.py", dags_dir)
    shutil.copy(REPO / "dags" / ".airflowignore", dags_dir)
    src = dags_dir / "sources"
    _source(src / "asset_driven", schedule={"cron": "15 * * * *", "assets": ["core/page"]}, view=False)
    _source(src / "cron_only", schedule="0 4 * * *", tags=["x"])
    _source(src / "switched_off", enabled=False)
    (src / "broken").mkdir()
    (src / "broken" / "source.yml").write_text("name: broken\nfetch: {type: nope}\n")
    (src / "_template").mkdir()
    (src / "_template" / "source.yml").write_text("ignored: true")

    bag = _dagbag(dags_dir)
    assert bag.import_errors == {}
    assert set(bag.dags) == {"source_asset_driven", "source_cron_only", "source_broken"}

    asset = bag.dags["source_asset_driven"]
    assert asset.timetable.__class__.__name__ == "AssetOrTimeSchedule"
    assert {t.task_id for t in asset.tasks} == {"land", "transform", "verify"}   # view: false
    land = asset.get_task("land")
    assert land.pool == "wiki_api"
    assert sorted(a.uri for a in asset.get_task("verify").outlets) == [
        "postgres://postgres:5432/birddog/core/document", "postgres://postgres:5432/birddog/core/page"]

    cron = bag.dags["source_cron_only"]
    assert cron.timetable.__class__.__name__ == "CronTriggerTimetable"
    assert set(cron.tags) == {"source", "x"}

    broken = bag.dags["source_broken"]
    assert "invalid" in broken.tags and [t.task_id for t in broken.tasks] == ["invalid_source_yml"]
    assert "fetch.type" in broken.doc_md


def test_invalid_task_raises_validation_message():
    from birddog.sources.dags import invalid_task
    with pytest.raises(ValueError, match="fetch.type"):
        invalid_task("broken/source.yml: fetch.type: required")
