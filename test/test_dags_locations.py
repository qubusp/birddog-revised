"""Parses dags/ (as test/test_dags.py does) and checks the location finder DAG
(dags/yaml/locations.yml) and its hook into app_refresh_page.

Needs Airflow and dag-factory, i.e. the Airflow image; skipped elsewhere:
    docker run --rm -v "$PWD":/opt/birddog -w /opt/birddog -e PYTHONPATH=/opt/birddog \\
        birddog-airflow:local python -m pytest -q test/test_dags_locations.py
"""

import os
import unittest

try:
    from airflow.models.dagbag import DagBag
except ImportError:  # not in the Airflow image
    DagBag = None

DAGS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dags")
QUEUE_ASSET = "postgres://postgres:5432/birddog/core/v_docs_no_location"


@unittest.skipIf(DagBag is None, "Airflow is not installed")
class LocationDagParseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bag = DagBag(dag_folder=DAGS_DIR)

    def test_no_import_errors(self):
        self.assertEqual(self.bag.import_errors, {})

    def test_locate_documents_shape(self):
        dag = self.bag.get_dag("locate_documents")
        self.assertIsNotNone(dag)
        # not set, so DAGS_ARE_PAUSED_AT_CREATION (true on the server) applies
        self.assertIsNot(dag.is_paused_upon_creation, False)
        self.assertEqual(dag.max_active_runs, 1)
        self.assertEqual(set(dag.params.keys()), {"limit"})
        self.assertEqual({t.task_id for t in dag.tasks}, {"batches", "locate"})
        locate = dag.get_task("locate")
        self.assertEqual(locate.upstream_task_ids, {"batches"})
        # only the LLM caller is in llm_api, so a run without the key (or
        # before the pool exists) still reaches the skip in `batches`
        self.assertEqual(locate.pool, "llm_api")
        self.assertEqual(dag.get_task("batches").pool, "default_pool")
        self.assertEqual(locate.execution_timeout.total_seconds(), 3600)

    def test_scheduled_hourly_and_on_queue_asset(self):
        dag = self.bag.get_dag("locate_documents")
        self.assertIn("core/v_docs_no_location", str(dag.timetable.asset_condition))
        self.assertIn("30 * * * *", str(dag.timetable.summary))

    def test_app_refresh_page_emits_queue_asset(self):
        dag = self.bag.get_dag("app_refresh_page")
        outlets = [getattr(o, "uri", None) for o in dag.get_task("subtree").outlets]
        self.assertIn(QUEUE_ASSET, outlets)
        # unchanged otherwise
        self.assertEqual({t.task_id for t in dag.tasks}, {"page", "subtree"})
        self.assertTrue(all(t.pool == "wiki_api" for t in dag.tasks))


if __name__ == "__main__":
    unittest.main()
