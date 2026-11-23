"""AirflowJobs (birddog/airflow_jobs.py), the app routes that use it, the
raw.wiki_change feed and the DAG task callables -- all without Airflow or
Postgres (clients and cursors are mocked)."""

import datetime
import unittest
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from itsdangerous import URLSafeTimedSerializer

from birddog.airflow_client import AirflowError, AirflowNotFound
from birddog.airflow_jobs import AirflowJobs, REFRESH_DAG, TRANSLATE_DAG, WATCHLIST_DAG


def _jobs():
    client = MagicMock()
    client.configured = True
    client.list_dag_runs.return_value = []
    client.trigger_dag.side_effect = lambda dag_id, conf: {"dag_run_id": "new", "state": "queued", "conf": conf}
    return AirflowJobs(client=client), client


class AirflowJobsTests(unittest.TestCase):
    def test_refresh_triggers_one_run_per_title(self):
        jobs, client = _jobs()
        jobs.request_page_refresh("Архів:ДАЗО/1", deep=True, update_locations=False)
        client.trigger_dag.assert_called_once_with(
            REFRESH_DAG, conf={"title": "Архів:ДАЗО/1", "deep": True, "update_locations": False})

        client.trigger_dag.reset_mock()
        jobs.request_page_refresh(["Архів:ДАЗО/1", "Архів:ДАЗО/2"])
        self.assertEqual(client.trigger_dag.call_count, 2)

    def test_same_request_while_running_is_not_triggered_twice(self):
        jobs, client = _jobs()
        conf = {"title": "Архів:ДАЗО/1"}
        client.list_dag_runs.return_value = [{"dag_run_id": "r1", "state": "running", "conf": conf}]
        run = jobs.request_translation("Архів:ДАЗО/1")
        self.assertEqual(run["dag_run_id"], "r1")
        client.trigger_dag.assert_not_called()
        client.list_dag_runs.assert_called_with(TRANSLATE_DAG, states=("queued", "running"), limit=100)

    def test_status_shape(self):
        jobs, client = _jobs()
        client.list_dag_runs.return_value = [{
            "dag_run_id": "manual__1", "state": "running", "start_date": "2026-11-23T10:00:00Z",
            "conf": {"title": "Архів:ДАЗО/1", "deep": True, "update_locations": False}}]
        client.get_task_instances.return_value = [{"state": "success"}, {"state": "running"}]
        tasks = jobs.status()
        self.assertEqual(tasks["manual__1"]["description"], "Архів:ДАЗО/1")
        self.assertEqual((tasks["manual__1"]["completed"], tasks["manual__1"]["total"]), (1, 2))
        self.assertTrue(tasks["manual__1"]["deep"])

    def test_status_when_airflow_down(self):
        jobs, client = _jobs()
        client.list_dag_runs.side_effect = AirflowError("down")
        self.assertEqual(jobs.status(), {})
        self.assertEqual(jobs.active_translations(), [])

    def test_active_translations(self):
        jobs, client = _jobs()
        client.list_dag_runs.return_value = [{"dag_run_id": "r", "state": "queued", "conf": {"title": "T"}}]
        client.get_task_instances.return_value = []
        self.assertEqual(jobs.active_translations(), [{"name": "T", "completed": 0, "length": 1}])

    def test_cancel(self):
        jobs, client = _jobs()
        client.get_dag_run.return_value = {"state": "running"}
        jobs.cancel("r1")
        client.set_dag_run_state.assert_called_once_with(REFRESH_DAG, "r1", "failed")

        client.get_dag_run.side_effect = AirflowNotFound("no", 404)
        with self.assertRaises(KeyError):
            jobs.cancel("nope")

    def test_watch_check(self):
        jobs, client = _jobs()
        jobs.request_watch_check("a@b.c", "Архів:ДАЛО")
        client.trigger_dag.assert_called_once_with(
            WATCHLIST_DAG, conf={"email": "a@b.c", "archive_title": "Архів:ДАЛО"})
        client.get_dag_run.return_value = {"state": "success"}
        self.assertEqual(jobs.watch_check_state("r"), "success")

    def test_available_follows_configuration(self):
        jobs, client = _jobs()
        client.configured = False
        self.assertFalse(jobs.available)


class WatchCheckRouteTests(unittest.TestCase):
    """/watchlist/<title>/check with the Airflow backend."""

    def setUp(self):
        import birddog.service as service
        from birddog.service import app
        self.service = service
        app.config["TESTING"] = True
        app.secret_key = "test_secret"
        service.serializer = URLSafeTimedSerializer(app.secret_key)

        self.jobs = MagicMock()
        self.jobs.available = True
        service.runtime = MagicMock(state="running", reads_from_database=True, jobs=self.jobs)
        self.user = MagicMock(email="u@example.com", role="user")
        self.user.get_watchlist.return_value = {}
        self.user.stored_unresolved.return_value = {"Архів:ДАЛО": {}}
        service.users = MagicMock()
        service.users.lookup.return_value = self.user

        self.client = app.test_client()
        with self.client.session_transaction() as sess:
            sess["user"] = {"email": "u@example.com", "name": "U"}

    def test_start_then_poll_then_result(self):
        self.jobs.request_watch_check.return_value = {"dag_run_id": "r1", "state": "queued"}
        resp = self.client.get("/watchlist/Архів:ДАЛО/check?tree")
        self.assertEqual(resp.status_code, 202)
        self.assertEqual(resp.get_json()["run_id"], "r1")
        self.jobs.request_watch_check.assert_called_once_with("u@example.com", "Архів:ДАЛО")

        self.jobs.watch_check_state.return_value = "running"
        resp = self.client.get("/watchlist/Архів:ДАЛО/check?tree&run_id=r1")
        self.assertEqual(resp.status_code, 202)

        self.jobs.watch_check_state.return_value = "success"
        resp = self.client.get("/watchlist/Архів:ДАЛО/check?tree&run_id=r1")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["unresolved"], {"Архів:ДАЛО": {}})
        self.user.stored_unresolved.assert_called_with("Архів:ДАЛО", tree=True)
        self.user.check_watchlist_item.assert_not_called()

    def test_failed_run(self):
        self.jobs.watch_check_state.return_value = "failed"
        resp = self.client.get("/watchlist/Архів:ДАЛО/check?run_id=r1")
        self.assertEqual(resp.status_code, 500)

    def test_not_watched(self):
        self.user.stored_unresolved.side_effect = KeyError("x")
        resp = self.client.get("/watchlist/Архів:ДАЛО/check")
        self.assertEqual(resp.status_code, 404)
        self.jobs.request_watch_check.assert_not_called()

    def test_airflow_down_returns_stored_alerts(self):
        self.jobs.request_watch_check.side_effect = AirflowError("down")
        resp = self.client.get("/watchlist/Архів:ДАЛО/check")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["success"])

    def test_translate_and_update_report_airflow_down(self):
        self.service.runtime.request_translation.side_effect = AirflowError("down")
        self.assertEqual(self.client.get("/translate?title=T").status_code, 503)
        self.user.role = "admin"
        self.service.runtime.update_to_database.side_effect = AirflowError("down")
        self.assertEqual(self.client.get("/database_update?title=T").status_code, 503)


@contextmanager
def _fake_connect(rows=(), rowcount=0):
    cur = MagicMock()
    cur.fetchall.return_value = list(rows)
    cur.rowcount = rowcount
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    yield conn


class WikiChangeFeedTests(unittest.TestCase):
    def test_latest_change_per_title_in_scope(self):
        from birddog.change_feed import WikiChangeFeed
        t = datetime.datetime
        rows = [
            ("Архів:ДАЛО/1", "edit", None, t(2026, 11, 20, 10, 0, 0), "alice"),
            ("Архів:ДАЛО/1", "edit", None, t(2026, 11, 21, 9, 30, 5), "bob"),
            ("Архів:ДАЛО/2", "move", "Архів:ДАЛО/3", t(2026, 11, 21, 11, 0, 0), None),
            ("Архів:ДАЗО/7", "edit", None, t(2026, 11, 21, 12, 0, 0), "carol"),
        ]
        with patch("birddog.change_feed.pg.connect", lambda: _fake_connect(rows)):
            updates = WikiChangeFeed().get_updates(["Архів:ДАЛО"], cutoff_date="2026-11-01T00:00:00Z")
        self.assertEqual(set(updates), {"Архів:ДАЛО/1", "Архів:ДАЛО/2"})
        self.assertEqual(updates["Архів:ДАЛО/1"],
                         {"timestamp": "2026-11-21T09:30:05Z", "user": "bob", "action": "edit"})
        self.assertEqual(updates["Архів:ДАЛО/2"]["target_title"], "Архів:ДАЛО/3")


class DagTaskTests(unittest.TestCase):
    def test_flag(self):
        from birddog.dag_tasks_app import _flag
        self.assertTrue(_flag("True"))
        self.assertTrue(_flag(True))
        self.assertFalse(_flag("False"))
        self.assertFalse(_flag(""))

    def test_shallow_refresh_skips_subtree(self):
        from birddog.dag_tasks_app import refresh_subtree
        self.assertIn("skipped", refresh_subtree("Архів:ДАЛО/1", deep="False"))

    def test_check_one_watch_uses_db_change_feed(self):
        from birddog import dag_tasks_app
        from birddog.change_feed import WikiChangeFeed
        with patch("birddog.user.check_watch", return_value={"a": {}, "b": {}}) as check:
            summary = dag_tasks_app.check_watches("u@example.com", "Архів:ДАЛО")
        self.assertEqual(summary["unresolved"], 2)
        email, title, runtime = check.call_args.args
        self.assertEqual((email, title), ("u@example.com", "Архів:ДАЛО"))
        self.assertIsInstance(runtime.update_manager, WikiChangeFeed)

    def test_check_all_watches_survives_one_failure(self):
        from birddog import dag_tasks_app
        watches = [{"email": "a@x", "title": "Архів:ДАЛО"}, {"email": "b@x", "title": "Архів:ДАЗО"}]
        with patch("birddog.watcher.get_all_active_watches", return_value=watches), \
             patch("birddog.user.check_watch", side_effect=[RuntimeError("boom"), {"x": {}}]):
            summary = dag_tasks_app.check_watches()
        self.assertEqual((summary["watches"], summary["errors"]), (1, 1))


if __name__ == "__main__":
    unittest.main()
