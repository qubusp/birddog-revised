"""Parses dags/ the way the dag-processor does (dag-factory through
dags/load_yaml_dags.py) and checks the DAGs the web app triggers.

Needs Airflow and dag-factory, i.e. the Airflow image; skipped elsewhere:
    docker run --rm -v "$PWD":/opt/birddog -w /opt/birddog -e PYTHONPATH=/opt/birddog \\
        birddog-airflow:local python -m pytest -q test/test_dags.py
"""

import os
import unittest

try:
    from airflow.models.dagbag import DagBag
except ImportError:  # not in the Airflow image
    DagBag = None

DAGS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dags")


@unittest.skipIf(DagBag is None, "Airflow is not installed")
class DagParseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bag = DagBag(dag_folder=DAGS_DIR)

    def test_no_import_errors(self):
        self.assertEqual(self.bag.import_errors, {})

    def test_app_dags_present_and_unpaused(self):
        for dag_id in ("app_refresh_page", "app_translate_page", "app_watchlist_check"):
            dag = self.bag.get_dag(dag_id)
            self.assertIsNotNone(dag, dag_id)
            self.assertFalse(dag.is_paused_upon_creation, dag_id)

    def test_refresh_uses_wiki_pool_and_params(self):
        dag = self.bag.get_dag("app_refresh_page")
        self.assertEqual({t.task_id for t in dag.tasks}, {"page", "subtree"})
        self.assertTrue(all(t.pool == "wiki_api" for t in dag.tasks))
        self.assertEqual(set(dag.params.keys()), {"title", "deep", "update_locations"})
        self.assertEqual(dag.get_task("subtree").upstream_task_ids, {"page"})

    def test_watchlist_check_runs_after_wiki_changes(self):
        dag = self.bag.get_dag("app_watchlist_check")
        self.assertEqual(set(dag.params.keys()), {"email", "archive_title"})
        # asset-scheduled on core.page, which wiki_changes updates
        self.assertIn("core/page", str(dag.timetable.asset_condition))

    def test_existing_dags_still_load(self):
        for dag_id in ("wiki_changes", "refresh_pages", "translate", "stack_check"):
            self.assertIsNotNone(self.bag.get_dag(dag_id), dag_id)


if __name__ == "__main__":
    unittest.main()
