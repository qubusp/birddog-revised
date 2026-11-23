"""birddog/dynamo_migrate.py against moto-faked DynamoDB/S3 and the scratch
Postgres DB (BIRDDOG_TEST_DATABASE_URL, see test/pg_scratch.py)."""

import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest import mock

try:
    from test.pg_scratch import require_test_database, truncate_app_tables
except ImportError:  # python -m unittest discover test
    from pg_scratch import require_test_database, truncate_app_tables

from werkzeug.security import generate_password_hash, check_password_hash

try:
    from moto import mock_aws
except ImportError:  # pragma: no cover
    mock_aws = None

REGION = "us-east-2"
PASSWORD_HASH = generate_password_hash("hunter2")


def setUpModule():
    if mock_aws is None:
        raise unittest.SkipTest("moto not installed")
    require_test_database()


def _kv_table(ddb, name):
    return ddb.create_table(
        TableName=name,
        KeySchema=[{"AttributeName": "namespace", "KeyType": "HASH"},
                   {"AttributeName": "key", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": "namespace", "AttributeType": "S"},
                              {"AttributeName": "key", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST")


def _log_table(ddb, name):
    return ddb.create_table(
        TableName=name,
        KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"},
                   {"AttributeName": "timestamp", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"},
                              {"AttributeName": "timestamp", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST")


def _pg_rows(sql, params=()):
    from birddog import app_db
    with app_db.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


class TestDynamoMigrate(unittest.TestCase):
    def setUp(self):
        truncate_app_tables()
        self._env = mock.patch.dict(os.environ, {
            "AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing",
            "AWS_DEFAULT_REGION": REGION, "BIRDDOG_NOCODB_ENV": "CLOUD_PROD"})
        self._env.start()
        self._aws = mock_aws()
        self._aws.start()
        import boto3
        from birddog import dynamo_migrate as dm
        self.dm = dm
        self.ddb = boto3.resource("dynamodb", region_name=REGION)
        self.s3 = boto3.client("s3", region_name=REGION)
        self.s3.create_bucket(Bucket=dm.S3_BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION})
        # titles coarsen through the wiki's archive registry; keep it predictable
        from birddog import user as user_mod
        self._root = mock.patch.object(user_mod, "archive_root", side_effect=lambda a, s: f"Archive:{a}")
        self._root.start()
        self._seed()

    def tearDown(self):
        self._root.stop()
        self._aws.stop()
        self._env.stop()

    def _put_user(self, email, record, prefix=""):
        self.s3.put_object(Bucket=self.dm.S3_BUCKET, Key=f"{prefix}users/{email}.json", Body=json.dumps(record))

    def _seed(self):
        self._put_user("ann@example.com", {"name": "Ann", "password": PASSWORD_HASH, "role": "admin"})
        self._put_user("bob@example.com", {
            "name": "Bob", "password": PASSWORD_HASH,
            # never logged in since the KV move: still inline
            "watchlist": {"DAZHO-D": {"cutoff_date": "2020,01,01,00:00"}},
            "preferences": {"theme": "dark"}})
        self._put_user("broken@example.com", {"name": "No password"})
        self._put_user("x@example.com", {"name": "STAGE only", "password": PASSWORD_HASH}, prefix="stage/")
        # not an account: sub-folders under users/
        self.s3.put_object(Bucket=self.dm.S3_BUCKET, Key="users/archive/old.json", Body="{}")

        kv = _kv_table(self.ddb, "birddog_key_value_store")
        with kv.batch_writer() as b:
            b.put_item(Item={"namespace": "wl:ann@example.com", "key": "Archive:ДАЖО",
                             "value": json.dumps({"cutoff_date": "2024-01-01T00:00:00Z",
                                                  "last_checked_date": "2026-11-01T10:00:00Z",
                                                  "include": ["Archive:ДАЖО"], "exclude": []})})
            # legacy pair-keyed entries for one archive merge to the older dates
            b.put_item(Item={"namespace": "wl:ann@example.com", "key": "DACHGO-D",
                             "value": json.dumps({"cutoff_date": "2021,06,01,00:00"})})
            b.put_item(Item={"namespace": "wl:ann@example.com", "key": "DACHGO-R",
                             "value": json.dumps({"cutoff_date": "2020,01,01,00:00"})})
            b.put_item(Item={"namespace": "wl:ann@example.com", "key": "__migrated__", "value": "1"})
            b.put_item(Item={"namespace": "wl:ghost@example.com", "key": "Archive:X",
                             "value": json.dumps({"cutoff_date": "2024-01-01T00:00:00Z", "include": ["Archive:X"]})})
            b.put_item(Item={"namespace": "pref:ann@example.com", "key": "last_page", "value": json.dumps("Archive:ДАЖО")})
            b.put_item(Item={"namespace": "pref:ann@example.com", "key": "hide_minor", "value": "true"})
            b.put_item(Item={"namespace": "pref:bob@example.com", "key": "theme", "value": json.dumps("light")})
            b.put_item(Item={"namespace": "archive_roots", "key": "Archive:ДАЖО",
                             "value": json.dumps({"label": "DAZHO"})})
        # a STAGE table that must not be read for PROD
        _kv_table(self.ddb, "birddog_key_value_store_STAGE").put_item(
            Item={"namespace": "pref:ann@example.com", "key": "theme", "value": json.dumps("stage")})

        watchers = _kv_table(self.ddb, "bd_watchers")
        with watchers.batch_writer() as b:
            for i in range(30):
                b.put_item(Item={"namespace": "u:ann@example.com:Archive:ДАЖО", "key": f"Archive:ДАЖО/{i}",
                                 "value": json.dumps({"modified": "2026-01-01T00:00:00Z"})})
            b.put_item(Item={"namespace": "w:ann@example.com", "key": "Archive:ДАЖО",
                             "value": json.dumps({"version": "v9", "include": ["Archive:ДАЖО"]})})

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        req = _log_table(self.ddb, "birddog_request_logs")
        svc = _log_table(self.ddb, "birddog_service_logs")
        for i in range(5):
            ts = (now - timedelta(hours=i)).isoformat()
            req.put_item(Item={"PK": "LOG", "timestamp": ts, "user": "abcd1234", "method": "GET",
                               "path": "/watchlist", "status": Decimal(200), "duration": Decimal(12 + i)})
            svc.put_item(Item={"PK": "LOG", "timestamp": ts, "resource": "KVStore", "method": "get",
                               "path": "wl:x", "size": Decimal(100), "duration": Decimal(3)})
        req.put_item(Item={"PK": "LOG", "timestamp": (now - timedelta(days=90)).isoformat(), "user": "old",
                           "method": "GET", "path": "/", "status": Decimal(200), "duration": Decimal(1)})

    def _counts(self, results):
        return {r["step"]: r["counts"] for r in results}

    def test_dry_run_counts_and_writes_nothing(self):
        results = self.dm.migrate_all(dry_run=True, include_logs=True, dynamodb=self.ddb, s3=self.s3)
        c = self._counts(results)
        self.assertEqual(c["users"]["source_users"], 3)
        self.assertEqual(c["users"]["users_inserted"], 2)
        self.assertEqual(c["users"]["users_skipped_invalid"], 1)
        # one transaction: the kv step sees the accounts the users step added
        self.assertEqual(c["kv"]["watches_inserted"], 2)
        self.assertEqual(c["kv"]["watches_skipped_no_account"], 1)
        self.assertEqual(c["kv"]["preferences_inserted"], 2)
        self.assertEqual(c["kv"]["preferences_updated"], 1)   # bob's inline theme, then KV wins
        self.assertEqual(c["kv"]["kv_inserted"], 1)
        self.assertEqual(c["kv_tables"]["bd_watchers_inserted"], 31)
        self.assertEqual(c["logs"]["birddog_request_logs_source_rows"], 5)  # 30-day window
        self.assertEqual(c["logs"]["birddog_service_logs_source_rows"], 5)
        for table in ("app.user_account", "app.watch", "app.user_preference", "app.kv",
                      "app.request_log", "app.service_log"):
            self.assertEqual(_pg_rows(f"SELECT count(*) FROM {table}")[0][0], 0, table)

    def test_apply_copies_everything_and_is_idempotent(self):
        first = self._counts(self.dm.migrate_all(dry_run=False, include_logs=True, dynamodb=self.ddb, s3=self.s3))
        self.assertEqual(first["logs"]["birddog_request_logs_inserted"], 5)

        accounts = dict((e, (n, h, r)) for e, n, h, r in
                        _pg_rows("SELECT email, name, password_hash, role FROM app.user_account"))
        self.assertEqual(set(accounts), {"ann@example.com", "bob@example.com"})
        self.assertEqual(accounts["ann@example.com"][2], "admin")
        self.assertTrue(check_password_hash(accounts["bob@example.com"][1], "hunter2"))

        from birddog import user as user_mod
        from birddog.user_store import PostgresUserStore
        with mock.patch.object(user_mod, "_pg_store", PostgresUserStore()):
            ann = user_mod.User.from_dict("ann@example.com", user_mod.load_user_record("ann@example.com"))
            self.assertTrue(ann.check_password("hunter2"))
            self.assertEqual(ann.get_watchlist(), {
                "Archive:DACHGO": {"cutoff_date": "2020-01-01T00:00:00Z",
                                   "include": ["Archive:DACHGO"], "exclude": []},
                "Archive:ДАЖО": {"cutoff_date": "2024-01-01T00:00:00Z",
                                 "last_checked_date": "2026-11-01T10:00:00Z",
                                 "include": ["Archive:ДАЖО"], "exclude": []},
            })
            self.assertEqual(ann.get_preference("last_page"), "Archive:ДАЖО")
            self.assertIs(ann.get_preference("hide_minor"), True)
            self.assertIsNone(ann.get_preference("theme"))  # STAGE table not read
            bob = user_mod.User.from_dict("bob@example.com", user_mod.load_user_record("bob@example.com"))
            self.assertEqual(list(bob.get_watchlist()), ["Archive:DAZHO"])
            self.assertEqual(bob.get_preference("theme"), "light")

        from birddog.store import PostgresKeyValueStore
        watchers = PostgresKeyValueStore(table_name="bd_watchers")
        self.assertEqual(watchers.count("u:ann@example.com:Archive:ДАЖО"), 30)
        self.assertEqual(json.loads(watchers.get("w:ann@example.com", "Archive:ДАЖО"))["version"], "v9")
        self.assertEqual(PostgresKeyValueStore().get("archive_roots", "Archive:ДАЖО"), json.dumps({"label": "DAZHO"}))

        logs = _pg_rows("SELECT user_id, status, duration_ms FROM app.request_log ORDER BY ts DESC")
        self.assertEqual(logs[0], ("abcd1234", 200, 12))

        # second run: nothing new, nothing duplicated
        second = self._counts(self.dm.migrate_all(dry_run=False, include_logs=True, dynamodb=self.ddb, s3=self.s3))
        self.assertNotIn("users_inserted", second["users"])
        self.assertEqual(second["users"]["users_updated"], 2)
        self.assertNotIn("watches_inserted", second["kv"])
        self.assertEqual(second["kv_tables"]["bd_watchers_inserted"], 0)
        self.assertEqual(second["kv_tables"]["bd_watchers_updated"], 31)
        self.assertEqual(second["logs"]["birddog_request_logs_inserted"], 0)
        self.assertEqual(_pg_rows("SELECT count(*) FROM app.request_log")[0][0], 5)
        self.assertEqual(_pg_rows("SELECT count(*) FROM app.watch")[0][0], 3)

    def test_stage_source_reads_stage_tables_and_prefix(self):
        c = self._counts(self.dm.migrate_app_state(dry_run=True, dynamodb=self.ddb, s3=self.s3,
                                                   source_env="CLOUD_STAGE", kv_tables=["bd_watchers"]))
        self.assertEqual(c["users"]["source_users"], 1)
        self.assertEqual(c["kv"]["source_rows"], 1)
        self.assertEqual(c["kv"]["preferences_skipped_no_account"], 1)
        self.assertEqual(c["kv_tables"]["bd_watchers_missing"], 1)

    def test_bad_rows_are_reported_not_fatal(self):
        self.ddb.Table("birddog_key_value_store").put_item(Item={
            "namespace": "wl:ann@example.com", "key": "Archive:Bad",
            "value": json.dumps({"cutoff_date": "not a date", "include": ["Archive:Bad"]})})
        results = self.dm.migrate_app_state(dry_run=False, dynamodb=self.ddb, s3=self.s3)
        kv = [r for r in results if r["step"] == "kv"][0]
        self.assertEqual(kv["counts"]["watches_failed"], 1)
        self.assertEqual(kv["counts"]["watches_inserted"], 2)
        self.assertTrue(any("Archive:Bad" in e for e in kv["errors"]))

    def test_dag_task_reads_params(self):
        from birddog import dag_tasks_users
        with mock.patch.object(self.dm, "dynamodb_resource", return_value=self.ddb), \
             mock.patch.object(self.dm, "s3_client", return_value=self.s3):
            out = dag_tasks_users.migrate_app_state(params={"apply": False, "kv_tables": ["bd_watchers"]})
            self.assertTrue(all(r["dry_run"] for r in out))
            self.assertEqual(dag_tasks_users.migrate_logs(params={}), {"step": "logs", "skipped": True})
            logs = dag_tasks_users.migrate_logs(params={"include_logs": True, "apply": True, "log_days": 120})
        self.assertEqual(logs["counts"]["birddog_request_logs_inserted"], 6)
        self.assertEqual(_pg_rows("SELECT count(*) FROM app.user_account")[0][0], 0)


if __name__ == "__main__":
    unittest.main()
