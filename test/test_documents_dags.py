"""dags/yaml/documents.yml builds the duplicate-linking and FTP-matching DAGs
with the expected shape. Needs Airflow and dag-factory (skipped without them),
not a database."""

from pathlib import Path

import pytest

pytest.importorskip("airflow")
pytest.importorskip("dagfactory")

REPO = Path(__file__).resolve().parents[1]
ASSET = "postgres://postgres:5432/birddog/core/"


@pytest.fixture(scope="module")
def bag(tmp_path_factory):
    import os
    os.environ.setdefault("AIRFLOW_HOME", str(tmp_path_factory.mktemp("airflow_home")))
    os.environ.setdefault("AIRFLOW__CORE__LOAD_EXAMPLES", "false")
    from airflow.dag_processing.dagbag import DagBag
    return DagBag(dag_folder=str(REPO / "dags"))


def test_parses(bag):
    assert bag.import_errors == {}
    assert {"link_duplicates", "match_ftp_documents", "refresh_doc_metadata"} <= set(bag.dags)


def test_link_duplicates_shape(bag):
    dag = bag.dags["link_duplicates"]
    tasks = {t.task_id: t for t in dag.tasks}
    assert set(tasks) == {"batches", "link"}
    assert tasks["link"].upstream_task_ids == {"batches"}
    assert [a.uri for a in tasks["link"].outlets] == [ASSET + "document_duplicate"]
    assert "core/document" in str(dag.timetable.asset_condition)
    assert dag.max_active_runs == 1 and not dag.catchup


def test_match_ftp_shape(bag):
    dag = bag.dags["match_ftp_documents"]
    tasks = {t.task_id: t for t in dag.tasks}
    assert set(tasks) == {"ftp_batches", "ftp_fingerprint", "doc_batches",
                          "doc_fingerprint", "reconcile"}
    assert tasks["ftp_fingerprint"].upstream_task_ids == {"ftp_batches"}
    assert tasks["doc_batches"].upstream_task_ids == {"ftp_fingerprint"}
    assert tasks["doc_fingerprint"].upstream_task_ids == {"doc_batches"}
    assert tasks["reconcile"].upstream_task_ids == {"doc_fingerprint"}
    # a skipped SFTP branch must not skip the rest
    assert tasks["doc_batches"].trigger_rule == "none_failed"
    assert tasks["reconcile"].trigger_rule == "none_failed"
    assert tasks["ftp_fingerprint"].max_active_tis_per_dag == 1
    assert tasks["doc_fingerprint"].pool == "wiki_api"
    assert [a.uri for a in tasks["reconcile"].outlets] == [ASSET + "ftp_document"]
    assert "core/ftp_entry" in str(dag.timetable.asset_condition)


def test_serialize(bag):
    from airflow.serialization.serialized_objects import DagSerialization
    for dag_id in ("link_duplicates", "match_ftp_documents"):
        DagSerialization.to_dict(bag.dags[dag_id])


def test_sftp_branch_skips_without_password(monkeypatch):
    from airflow.sdk.exceptions import AirflowSkipException
    from birddog import dag_tasks_docs
    monkeypatch.delenv("SFTP_PASSWORD", raising=False)
    with pytest.raises(AirflowSkipException):
        dag_tasks_docs.ftp_fingerprint_batches()
