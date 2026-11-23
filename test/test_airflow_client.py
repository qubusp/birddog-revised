import unittest
from unittest.mock import MagicMock

from birddog.airflow_client import (
    AirflowClient,
    AirflowError,
    AirflowNotFound,
    run_progress,
)


def _resp(status=200, payload=None):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = payload if payload is not None else {}
    resp.content = b"{}" if payload is not None else b""
    resp.text = str(payload)
    return resp


class AirflowClientTests(unittest.TestCase):
    def make_client(self):
        session = MagicMock()
        session.post.return_value = _resp(201, {"access_token": "tok1"})
        client = AirflowClient("http://airflow:8080/", "svc", "pw", session=session)
        return client, session

    def test_not_configured(self):
        client = AirflowClient(None, None, None, session=MagicMock())
        self.assertFalse(client.configured)
        with self.assertRaises(AirflowError):
            client.get_dag_run("d", "r")

    def test_login_then_bearer_token(self):
        client, session = self.make_client()
        session.request.return_value = _resp(200, {"dag_run_id": "r1", "state": "running"})
        run = client.get_dag_run("app_refresh_page", "r1")
        self.assertEqual(run["state"], "running")
        session.post.assert_called_once_with(
            "http://airflow:8080/auth/token", json={"username": "svc", "password": "pw"}, timeout=10)
        method, url = session.request.call_args.args
        self.assertEqual((method, url), ("GET", "http://airflow:8080/api/v2/dags/app_refresh_page/dagRuns/r1"))
        self.assertEqual(session.request.call_args.kwargs["headers"], {"Authorization": "Bearer tok1"})
        # token is reused
        client.get_dag_run("app_refresh_page", "r1")
        self.assertEqual(session.post.call_count, 1)

    def test_expired_token_is_renewed_once(self):
        client, session = self.make_client()
        session.post.side_effect = [_resp(201, {"access_token": "old"}), _resp(201, {"access_token": "new"})]
        session.request.side_effect = [_resp(401, {"detail": "expired"}), _resp(200, {"state": "success"})]
        self.assertEqual(client.get_dag_run("d", "r")["state"], "success")
        self.assertEqual(session.request.call_args.kwargs["headers"], {"Authorization": "Bearer new"})

    def test_bad_credentials(self):
        client, session = self.make_client()
        session.post.return_value = _resp(401, {"detail": "Invalid credentials"})
        with self.assertRaises(AirflowError) as ctx:
            client.get_dag_run("d", "r")
        self.assertEqual(ctx.exception.status, 401)

    def test_unreachable(self):
        import requests
        client, session = self.make_client()
        session.post.side_effect = requests.ConnectionError("refused")
        with self.assertRaises(AirflowError):
            client.get_dag_run("d", "r")

    def test_trigger_dag_body(self):
        client, session = self.make_client()
        session.request.return_value = _resp(200, {"dag_run_id": "manual__1", "state": "queued"})
        run = client.trigger_dag("app_translate_page", conf={"title": "T"})
        self.assertEqual(run["dag_run_id"], "manual__1")
        self.assertEqual(session.request.call_args.args,
                         ("POST", "http://airflow:8080/api/v2/dags/app_translate_page/dagRuns"))
        self.assertEqual(session.request.call_args.kwargs["json"],
                         {"logical_date": None, "conf": {"title": "T"}})

    def test_list_dag_runs_params(self):
        client, session = self.make_client()
        session.request.return_value = _resp(200, {"dag_runs": [{"dag_run_id": "a"}], "total_entries": 1})
        runs = client.list_dag_runs("d", states=("queued", "running"), limit=5)
        self.assertEqual(runs, [{"dag_run_id": "a"}])
        self.assertEqual(session.request.call_args.kwargs["params"],
                         {"limit": 5, "order_by": "-run_after", "state": ["queued", "running"]})

    def test_task_instances_and_state_patch(self):
        client, session = self.make_client()
        session.request.return_value = _resp(200, {"task_instances": [{"state": "success"}]})
        self.assertEqual(client.get_task_instances("d", "r"), [{"state": "success"}])
        session.request.return_value = _resp(200, {"state": "failed"})
        client.set_dag_run_state("d", "r", "failed")
        self.assertEqual(session.request.call_args.args[0], "PATCH")
        self.assertEqual(session.request.call_args.kwargs["json"], {"state": "failed"})

    def test_not_found_and_errors(self):
        client, session = self.make_client()
        session.request.return_value = _resp(404, {"detail": "no"})
        with self.assertRaises(AirflowNotFound):
            client.get_dag_run("d", "missing")
        session.request.return_value = _resp(409, {"detail": "exists"})
        with self.assertRaises(AirflowError) as ctx:
            client.trigger_dag("d")
        self.assertEqual(ctx.exception.status, 409)

    def test_run_progress(self):
        tis = [{"state": "success"}, {"state": "running"}, {"state": "skipped"}, {"state": None}]
        self.assertEqual(run_progress(tis), (2, 4))
        self.assertEqual(run_progress([]), (0, 0))


if __name__ == "__main__":
    unittest.main()
