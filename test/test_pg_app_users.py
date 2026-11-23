"""Postgres backends for the web app's own state (store.py, log.py, user.py).

Runs against the scratch DB named by BIRDDOG_TEST_DATABASE_URL (see
test/pg_scratch.py); skipped without it."""

import os
import json
import threading
import unittest
import uuid
from datetime import datetime, timedelta, UTC
from unittest import mock

try:
    from test.pg_scratch import require_test_database, truncate_app_tables
    from test import test_store
except ImportError:  # python -m unittest discover test
    from pg_scratch import require_test_database, truncate_app_tables
    import test_store

from werkzeug.security import generate_password_hash


def setUpModule():
    require_test_database()


class PgTestCase(unittest.TestCase):
    def setUp(self):
        truncate_app_tables()


class TestStoreBackendSelection(unittest.TestCase):
    def test_explicit_backend_wins(self):
        from birddog.env import store_backend
        with mock.patch.dict(os.environ, {"BIRDDOG_STORE_BACKEND": "sqlite", "BIRDDOG_DATABASE_URL": "postgresql://x/y"}):
            self.assertEqual(store_backend(), "sqlite")

    def test_database_url_means_postgres(self):
        from birddog.env import store_backend
        with mock.patch.dict(os.environ, {"BIRDDOG_DATABASE_URL": "postgresql://x/y"}):
            os.environ.pop("BIRDDOG_STORE_BACKEND", None)
            self.assertEqual(store_backend(), "postgres")

    def test_bad_backend_rejected(self):
        from birddog.env import store_backend
        with mock.patch.dict(os.environ, {"BIRDDOG_STORE_BACKEND": "redis"}):
            with self.assertRaises(ValueError):
                store_backend()

    def test_base_table_name_strips_stage_suffix(self):
        from birddog.env import base_table_name
        self.assertEqual(base_table_name("bd_watchers_STAGE"), "bd_watchers")
        self.assertEqual(base_table_name("bd_watchers"), "bd_watchers")


class TestPostgresKeyValueStore(PgTestCase):
    _assert_eventually = test_store.TestStores._assert_eventually

    def test_common_kv_contract(self):
        from birddog.store import PostgresKeyValueStore
        test_store.TestStores._run_common_kv_store_tests(self, PostgresKeyValueStore(table_name="unittest_kv"), "postgres")

    def test_stores_are_separate_and_stage_suffix_dropped(self):
        from birddog.store import PostgresKeyValueStore
        a = PostgresKeyValueStore(table_name="bd_watchers_STAGE")
        b = PostgresKeyValueStore()
        self.assertEqual(a.store, "bd_watchers")
        a.insert("ns", "k", "from-a")
        b.insert("ns", "k", "from-b")
        self.assertEqual(a.get("ns", "k"), "from-a")
        self.assertEqual(b.get("ns", "k"), "from-b")
        self.assertEqual(PostgresKeyValueStore(table_name="bd_watchers").get("ns", "k"), "from-a")

    def test_bulk_and_ordering(self):
        from birddog.store import PostgresKeyValueStore
        kv = PostgresKeyValueStore(table_name="unittest_bulk")
        items = {f"k{i:05d}": json.dumps({"i": i}) for i in range(2500)}
        kv.insert_many("ns", items)
        self.assertEqual(kv.count("ns"), 2500)
        keys = [k for k, _ in kv.get_all("ns")]
        self.assertEqual(keys, sorted(keys))
        got = kv.get_many("ns", ["k00001", "k02499", "missing"])
        self.assertEqual(set(got), {"k00001", "k02499"})
        kv.remove_many("ns", list(items)[:500])
        self.assertEqual(kv.count("ns"), 2000)
        self.assertFalse(kv.remove_if_exists("ns", "k00000"))
        self.assertTrue(kv.remove_if_exists("ns", "k00500"))
        kv.update_if_exists("ns", "nope", "x")
        with self.assertRaises(KeyError):
            kv.get("ns", "nope")

    def test_type_checks(self):
        from birddog.store import PostgresKeyValueStore
        kv = PostgresKeyValueStore(table_name="unittest_types")
        with self.assertRaises(TypeError):
            kv.insert("ns", "k", 5)


class TestPostgresStringQueue(PgTestCase):
    _assert_eventually = test_store.TestStores._assert_eventually

    def test_common_queue_contract(self):
        from birddog.store import PostgresStringQueue
        test_store.TestStores._run_common_string_queue_tests(self, PostgresStringQueue(table_name="unittest_q"), "postgres")

    def test_concurrent_claims_never_overlap(self):
        from birddog.store import PostgresStringQueue
        q = PostgresStringQueue(table_name="unittest_q2")
        q.append("work", [f"item{i}" for i in range(200)])
        claimed = []
        lock = threading.Lock()

        def worker(n):
            while True:
                got = q.claim("work", 7, lease_ms=60_000, consumer_id=f"c{n}")
                if not got:
                    return
                with lock:
                    claimed.extend(c.value for c in got)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(claimed), sorted(f"item{i}" for i in range(200)))


class TestPostgresLoggers(PgTestCase):
    def test_event_logger_roundtrip_and_truncate(self):
        from birddog.log import PostgresEventLogger, summarize_duration_by_path_group, user_histogram
        log = PostgresEventLogger()
        log.log_request("abc12345", "GET", "/watchlist", 200, 0.0123)
        log.log_request("abc12345", "POST", "/watchlist/x", 201, 0.5)
        log.log_request("unknown", "GET", "/", 200, 0.001)
        now = datetime.now(UTC)
        df = log.load_logs(now - timedelta(minutes=5), now + timedelta(seconds=5))
        self.assertEqual(len(df), 3)
        self.assertEqual(user_histogram(df).to_dict(), {"abc12345": 2, "unknown": 1})
        self.assertEqual(int(df.loc[df["path"] == "/watchlist/x", "duration"].iloc[0]), 500)
        summary = summarize_duration_by_path_group(df)
        self.assertEqual(int(summary.loc["/watchlist", "count"]), 2)
        self.assertEqual(log.truncate(now + timedelta(seconds=5)), 3)
        self.assertTrue(log.load_logs(now - timedelta(days=1), now + timedelta(days=1)).empty)

    def test_service_logger_roundtrip_and_summary(self):
        from birddog.log import PostgresServiceLogger, ServiceLogger
        log = PostgresServiceLogger()
        log.log_service_call("KVStore", "get", "ns", 10, 0.002)
        log.log_service_call("KVStore", "insert", "ns", 30, 0.004)
        log.log_service_call("S3", "load", "p", None, None)
        now = datetime.now(UTC)
        df = log.load_logs(now - timedelta(minutes=5), now + timedelta(seconds=5))
        self.assertEqual(len(df), 3)
        summary = ServiceLogger.summarize_service_usage(df, by="resource", sample_interval_minutes=5)
        kv = summary[summary["resource"] == "KVStore"].iloc[0]
        self.assertEqual(int(kv["count"]), 2)
        self.assertEqual(int(kv["cumulative_size"]), 40)
        # ISO strings work as bounds too (the kill switch/UI pass datetimes)
        self.assertEqual(len(log.load_logs((now - timedelta(minutes=5)).isoformat(), now.isoformat())), 3)

    def test_log_write_failure_is_swallowed(self):
        from birddog import app_db
        from birddog.log import PostgresEventLogger
        with mock.patch.object(app_db, "cursor", side_effect=RuntimeError("db down")):
            PostgresEventLogger().log_request("u", "GET", "/", 200, 0.1)  # must not raise


class TestPostgresUsers(PgTestCase):
    """user.py and service.Users against app.user_account/watch/user_preference."""

    def setUp(self):
        super().setUp()
        from birddog import user as user_mod
        from birddog.user_store import PostgresUserStore
        self.user_mod = user_mod
        self.watcher_removed = []
        self._patches = [
            mock.patch.object(user_mod, "_pg_store", PostgresUserStore()),
            mock.patch.object(user_mod.watcher, "remove_watcher",
                              side_effect=lambda e, t: self.watcher_removed.append((e, t))),
        ]
        for p in self._patches:
            p.start()
        user_mod._global_user_locks.clear()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()

    def _new_user(self, email="ann@example.com", password="pw1"):
        u = self.user_mod.User(name="Ann", email=email, password=password)
        u.save()
        return u

    def test_account_roundtrip_and_passwords(self):
        u = self._new_user()
        rec = self.user_mod.load_user_record("ann@example.com")
        self.assertEqual(rec["name"], "Ann")
        self.assertEqual(rec["role"], "user")
        loaded = self.user_mod.User.from_dict("ann@example.com", rec)
        self.assertTrue(loaded.check_password("pw1"))
        self.assertFalse(loaded.check_password("nope"))
        self.assertTrue(loaded.change_password("pw1", "pw2"))
        again = self.user_mod.User.from_dict("ann@example.com", self.user_mod.load_user_record("ann@example.com"))
        self.assertTrue(again.check_password("pw2"))
        again.set_role("admin")
        self.assertEqual(self.user_mod.load_user_record("ann@example.com")["role"], "admin")
        self.assertIsNone(self.user_mod.load_user_record("nobody@example.com"))

    def test_existing_hashes_keep_working(self):
        # hashes as the old app stored them on S3 (werkzeug scrypt / pbkdf2)
        from birddog.user_store import PostgresUserStore
        store = PostgresUserStore()
        for i, method in enumerate(("scrypt", "pbkdf2:sha256", "pbkdf2:sha256:600000")):
            email = f"old{i}@example.com"
            store.save_user(email, {"name": "Old", "password": generate_password_hash("s3cret", method=method)})
            u = self.user_mod.User.from_dict(email, store.load_user(email))
            self.assertTrue(u.check_password("s3cret"), method)

    def test_watchlist_crud(self):
        u = self._new_user()
        title = "Archive:ДАЖО"
        with mock.patch.object(self.user_mod, "canonicalize_title", side_effect=lambda t: t):
            u.add_to_watchlist(title, "2024-01-01T00:00:00Z")
            wl = u.get_watchlist()
            self.assertEqual(wl, {title: {"cutoff_date": "2024-01-01T00:00:00Z", "include": [title], "exclude": []}})
            # an older cutoff merges down, a newer one does not
            u.add_to_watchlist(title, "2023-06-01T12:30:00Z")
            u.add_to_watchlist(title, "2025-01-01T00:00:00Z")
            self.assertEqual(u.get_watchlist()[title]["cutoff_date"], "2023-06-01T12:30:00Z")
            self.user_mod._save_watch_item(u.email, title, {
                **u.get_watchlist()[title], "last_checked_date": "2026-11-20T08:00:00Z"})
            self.assertEqual(self.user_mod._get_watch_item(u.email, title)["last_checked_date"], "2026-11-20T08:00:00Z")
            self.assertTrue(u.remove_from_watchlist(title))
            self.assertEqual(u.get_watchlist(), {})
            self.assertEqual(self.watcher_removed, [(u.email, title)])
            with self.assertRaises(KeyError):
                self.user_mod._get_watch_item(u.email, title)

    def test_check_watchlist_item_records_last_checked(self):
        u = self._new_user()
        title = "Archive:ДАЖО"
        calls = []

        def fake_check(email, t, runtime, include, cutoff_date, exclude=None):
            calls.append((t, include, cutoff_date, exclude))
            return {"Archive:ДАЖО/1": {"modified": "2026-01-01T00:00:00Z"}}

        with mock.patch.object(self.user_mod, "canonicalize_title", side_effect=lambda t: t), \
             mock.patch.object(self.user_mod.watcher, "check_watcher", side_effect=fake_check):
            u.add_to_watchlist(title, "2024-01-01T00:00:00Z")
            out = u.check_watchlist_item(title)
        self.assertEqual(out, [{"name": "Archive:ДАЖО/1", "modified": "2026-01-01T00:00:00Z"}])
        self.assertEqual(calls, [(title, [title], "2024-01-01T00:00:00Z", [])])
        self.assertIn("last_checked_date", u.get_watchlist()[title])

    def test_preferences(self):
        u = self._new_user()
        self.assertEqual(u.get_preference("theme", "light"), "light")
        for key, value in [("theme", "dark"), ("page", 50), ("hide", True), ("obj", {"a": [1, 2]}), ("s", "123")]:
            u.set_preference(key, value)
            self.assertEqual(u.get_preference(key), value)
        u.set_preference("theme", "blue")
        self.assertEqual(u.get_preference("theme"), "blue")

    def test_legacy_embedded_watchlist_and_preferences(self):
        with mock.patch.object(self.user_mod, "archive_root", side_effect=lambda a, s: f"Archive:{a}"):
            u = self.user_mod.User.from_dict("leg@example.com", {
                "name": "Leg", "password": generate_password_hash("pw"),
                "watchlist": {"DACHGO-D": {"cutoff_date": "2021,06,01,00:00"},
                              "DACHGO-R": {"cutoff_date": "2020,01,01,00:00", "last_checked_date": "2020,02,01,00:00"}},
                "preferences": {"theme": "dark"},
            })
        wl = u.get_watchlist()
        self.assertEqual(list(wl), ["Archive:DACHGO"])
        self.assertEqual(wl["Archive:DACHGO"]["cutoff_date"], "2020-01-01T00:00:00Z")
        self.assertEqual(wl["Archive:DACHGO"]["last_checked_date"], "2020-02-01T00:00:00Z")
        self.assertEqual(u.get_preference("theme"), "dark")
        self.assertNotIn("watchlist", self.user_mod.load_user_record("leg@example.com"))

    def test_service_users_signup_login(self):
        from birddog import service
        mgr = service.Users()
        created = mgr.create("new@example.com", "New", "pw")
        self.assertIsNotNone(created)
        self.assertIsNone(mgr.create("new@example.com", "Again", "pw"))
        self.assertIsNotNone(mgr.login("new@example.com", "pw"))
        self.assertIsNone(mgr.login("new@example.com", "bad"))
        self.assertIsNone(mgr.login("ghost@example.com", "pw"))


if __name__ == "__main__":
    unittest.main()
