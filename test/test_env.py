import os
import unittest
from unittest.mock import patch

from birddog.env import env_table_name


class TestEnvTableName(unittest.TestCase):
    def test_stage_gets_suffix(self):
        with patch.dict(os.environ, {"BIRDDOG_NOCODB_ENV": "CLOUD_STAGE"}):
            self.assertEqual(env_table_name("bd_task_queue"), "bd_task_queue_STAGE")

    def test_prod_and_local_keep_original_names(self):
        for env in ("CLOUD_PROD", "LOCAL"):
            with self.subTest(env=env), patch.dict(os.environ, {"BIRDDOG_NOCODB_ENV": env}):
                self.assertEqual(env_table_name("bd_task_queue"), "bd_task_queue")

    def test_unset_env_keeps_original_name(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(env_table_name("bd_doc_tracker"), "bd_doc_tracker")

    def test_stage_names_stay_inside_iam_pattern(self):
        # the EB role's DynamoDB policy only covers "bd_*" / "birddog_*" tables
        with patch.dict(os.environ, {"BIRDDOG_NOCODB_ENV": "CLOUD_STAGE"}):
            for name in ("bd_task_queue", "bd_task_kv_store", "bd_db_update",
                         "bd_tracker_kv_store", "bd_doc_tracker"):
                with self.subTest(name=name):
                    self.assertTrue(env_table_name(name).startswith("bd_"))

    def test_default_dynamodb_kv_table_follows_env(self):
        from birddog import store
        for env, expected in (("CLOUD_STAGE", "birddog_key_value_store_STAGE"),
                              ("CLOUD_PROD", "birddog_key_value_store")):
            with self.subTest(env=env), patch.dict(os.environ, {"BIRDDOG_NOCODB_ENV": env}), \
                 patch("boto3.resource"), patch("boto3.client"), \
                 patch.object(store.DynamoDBKeyValueStore, "_ensure_table_exists"):
                self.assertEqual(store.DynamoDBKeyValueStore()._table_name, expected)

    def test_task_manager_uses_env_tables(self):
        from birddog import task
        created = []
        with patch.dict(os.environ, {"BIRDDOG_NOCODB_ENV": "CLOUD_STAGE"}), \
             patch.object(task, "StringQueue", side_effect=lambda table_name: created.append(table_name)), \
             patch.object(task, "KeyValueStore", side_effect=lambda table_name: created.append(table_name)):
            task.TaskManager("EnvTableTest")
        self.assertEqual(created, ["bd_task_queue_STAGE", "bd_task_kv_store_STAGE"])


class TestModeMismatch(unittest.TestCase):
    def _mismatch(self, env):
        from birddog.env import mode_mismatch
        with patch.dict(os.environ, env, clear=True):
            return mode_mismatch()

    def test_consistent_modes_are_quiet(self):
        self.assertIsNone(self._mismatch({}))
        self.assertIsNone(self._mismatch({"BIRDDOG_DATABASE_URL": "postgresql://x/y"}))
        self.assertIsNone(self._mismatch({"BIRDDOG_DATABASE_URL": "postgresql://x/y",
                                          "BIRDDOG_STORE_BACKEND": "postgres"}))
        self.assertIsNone(self._mismatch({"BIRDDOG_STORE_BACKEND": "sqlite"}))
        self.assertIsNone(self._mismatch({"BIRDDOG_AWS_ENVIRONMENT": "1"}))

    def test_database_with_non_postgres_store(self):
        for backend in ("sqlite", "dynamodb"):
            with self.subTest(backend=backend):
                message = self._mismatch({"BIRDDOG_DATABASE_URL": "postgresql://x/y",
                                          "BIRDDOG_STORE_BACKEND": backend})
                self.assertIn(f"BIRDDOG_STORE_BACKEND={backend}", message)
                self.assertIn("BIRDDOG_DATABASE_URL is set", message)

    def test_postgres_store_without_database(self):
        message = self._mismatch({"BIRDDOG_STORE_BACKEND": "postgres"})
        self.assertIn("BIRDDOG_DATABASE_URL is not set", message)


if __name__ == "__main__":
    unittest.main()
