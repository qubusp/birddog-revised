"""Tests for birddog/alert_store.py: watcher alert state in app.watch.watcher
and app.alert_state (migration 016), the one-time copy from app.kv, and the
016 grants.

watcher.py's behaviour on this store is covered by the Postgres* variants in
test_watcher.py. The database tests need BIRDDOG_TEST_DATABASE_URL (see
test/pg_scratch.py)."""

import json
import os
import unittest
from unittest import mock

from birddog import alert_store
from birddog.alert_store import AlertStateStore, _parse_namespace

try:
    from test.pg_scratch import require_test_database, truncate_app_tables
except ImportError:
    from pg_scratch import require_test_database, truncate_app_tables

EMAIL = "alerts_test@example.com"
TITLE = "Архів:ДААРК"
ITEM = f"{TITLE}/1"


class TestNamespaces(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(_parse_namespace(f"w:{EMAIL}"), ("w", EMAIL, None))
        self.assertEqual(_parse_namespace(f"u:{EMAIL}:{TITLE}"), ("u", EMAIL, TITLE))
        # the archive title has its own colon
        self.assertEqual(_parse_namespace(f"r:{EMAIL}:Архів:X/1"), ("r", EMAIL, "Архів:X/1"))
        self.assertEqual(_parse_namespace("active"), ("active", None, None))
        for bad in ("x:1", "u:nocolon", "w:", ""):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                _parse_namespace(bad)

    def test_watcher_uses_alert_store_on_postgres(self):
        from birddog import watcher
        with mock.patch.dict(os.environ, {"BIRDDOG_STORE_BACKEND": "postgres"}):
            self.assertIsInstance(watcher._make_watcher_store(), AlertStateStore)
        with mock.patch.dict(os.environ, {"BIRDDOG_STORE_BACKEND": "sqlite", "BIRDDOG_DATABASE_URL": ""}):
            self.assertNotIsInstance(watcher._make_watcher_store(), AlertStateStore)


class _DatabaseTest(unittest.TestCase):
    def setUp(self):
        require_test_database()
        truncate_app_tables()
        self.addCleanup(truncate_app_tables)
        from birddog import app_db
        self.app_db = app_db

    def sql(self, query, params=()):
        with self.app_db.cursor() as cur:
            cur.execute(query, params)
            return cur.fetchall() if cur.description else None

    def watch(self, email=EMAIL, title=TITLE):
        self.sql("INSERT INTO app.user_account (email, name, password_hash) VALUES (%s, 'n', 'x') "
                 "ON CONFLICT DO NOTHING", (email,))
        self.sql("INSERT INTO app.watch (email, archive_title, include) VALUES (%s, %s, ARRAY[%s])",
                 (email, title, title))

    def kv(self, namespace, key, value, store="bd_watchers"):
        self.sql("INSERT INTO app.kv (store, namespace, key, value) VALUES (%s, %s, %s, %s)",
                 (store, namespace, key, json.dumps(value)))


class TestAlertStateStore(_DatabaseTest):
    def test_state_only_for_watched_titles(self):
        store = AlertStateStore()
        store.insert(f"w:{EMAIL}", TITLE, json.dumps({"include": [TITLE]}))
        store.insert(f"u:{EMAIL}:{TITLE}", ITEM, json.dumps({"modified": "2026-01-01T00:00:00Z"}))
        with self.assertRaises(KeyError):
            store.get(f"w:{EMAIL}", TITLE)
        self.assertEqual(store.get_all(f"u:{EMAIL}:{TITLE}"), [])
        self.assertEqual(self.sql("SELECT count(*) FROM app.alert_state")[0][0], 0)

    def test_removing_the_watch_drops_its_state(self):
        self.watch()
        store = AlertStateStore()
        store.insert(f"w:{EMAIL}", TITLE, json.dumps({"include": [TITLE]}))
        store.insert(f"u:{EMAIL}:{TITLE}", ITEM, json.dumps({"modified": "x"}))
        self.sql("DELETE FROM app.watch WHERE email = %s", (EMAIL,))
        self.assertEqual(self.sql("SELECT count(*) FROM app.alert_state")[0][0], 0)
        self.assertEqual(store.get_all("active"), [])

    def test_one_row_per_item_with_derived_columns(self):
        self.watch()
        store = AlertStateStore()
        u, r = f"u:{EMAIL}:{TITLE}", f"r:{EMAIL}:{TITLE}"
        history = [{"modified": "2026-01-01T00:00:00Z", "last_resolved": "2026-01-02T00:00:00Z"}]
        store.insert(r, ITEM, json.dumps(history))
        store.insert(u, ITEM, json.dumps({"modified": "2026-02-01T00:00:00Z", "insignificant": True,
                                          "sig_span": ["a", "b"]}))
        rows = self.sql("""SELECT resolved, to_char(modified AT TIME ZONE 'UTC', 'YYYY-MM-DD'),
                                  to_char(last_resolved AT TIME ZONE 'UTC', 'YYYY-MM-DD'),
                                  significance, sig_span, history FROM app.alert_state""")
        self.assertEqual(rows, [(False, "2026-02-01", "2026-01-02", "insignificant", ["a", "b"], history)])

        # resolving: the unresolved part goes, the history stays
        store.remove_if_exists(u, ITEM)
        rows = self.sql("SELECT resolved, entry, significance, "
                        "to_char(modified AT TIME ZONE 'UTC', 'YYYY-MM-DD') FROM app.alert_state")
        self.assertEqual(rows, [(True, {}, None, "2026-01-01")])
        # clearing the history too deletes the row
        self.assertTrue(store.remove_if_exists(r, ITEM))
        self.assertFalse(store.remove_if_exists(r, ITEM))
        self.assertEqual(self.sql("SELECT count(*) FROM app.alert_state")[0][0], 0)

    def test_bad_dates_do_not_fail_the_write(self):
        self.watch()
        store = AlertStateStore()
        store.insert(f"u:{EMAIL}:{TITLE}", ITEM, json.dumps({"modified": "sometime in 1850"}))
        self.assertEqual(json.loads(store.get(f"u:{EMAIL}:{TITLE}", ITEM)), {"modified": "sometime in 1850"})
        self.assertEqual(self.sql("SELECT modified FROM app.alert_state"), [(None,)])

    def test_header_sets_moved_to(self):
        self.watch()
        store = AlertStateStore()
        store.insert(f"w:{EMAIL}", TITLE, json.dumps({"include": [TITLE], "moved_to": "Архів:Y"}))
        self.assertEqual(self.sql("SELECT moved_to FROM app.watch"), [("Архів:Y",)])
        store.remove_if_exists(f"w:{EMAIL}", TITLE)
        self.assertEqual(self.sql("SELECT moved_to, watcher FROM app.watch"), [(None, None)])

    def test_get_all_is_in_byte_order(self):
        self.watch()
        store = AlertStateStore()
        items = {f"{TITLE}/{k}": json.dumps({"modified": k}) for k in ("b", "B", "10", "2", "а")}
        store.insert_many(f"u:{EMAIL}:{TITLE}", items)
        self.assertEqual([k for k, _ in store.get_all(f"u:{EMAIL}:{TITLE}")], sorted(items))


class TestCopyFromKv(_DatabaseTest):
    def test_copy(self):
        other = "Архів:ДАЖО"
        self.watch()
        header = {"version": "v9", "include": [TITLE], "exclude": [],
                  "cutoff_date": "2025-01-01T00:00:00Z", "last_checked_date": "2025-06-01T00:00:00Z"}
        self.kv(f"w:{EMAIL}", TITLE, header)
        self.kv(f"u:{EMAIL}:{TITLE}", ITEM, {"modified": "2026-01-01T00:00:00Z", "new_page": True})
        self.kv(f"r:{EMAIL}:{TITLE}", ITEM, [{"modified": "2025-12-01T00:00:00Z",
                                              "last_resolved": "2025-12-02T00:00:00Z"}])
        self.kv(f"r:{EMAIL}:{TITLE}", f"{TITLE}/2", [{"modified": "m", "last_resolved": "l"}])
        self.kv("active", f"{EMAIL}:{TITLE}", {"email": EMAIL, "title": TITLE})
        # not on the watchlist: not copied, kept in the _copied store
        self.kv(f"w:{EMAIL}", other, header)
        self.kv(f"u:{EMAIL}:{other}", f"{other}/1", {"modified": "x"})
        # another app.kv store is left alone
        self.kv("ns", "k", "v", store="bd_other")

        store = AlertStateStore()
        self.assertEqual(json.loads(store.get(f"w:{EMAIL}", TITLE)), header)
        self.assertEqual(dict((k, json.loads(v)) for k, v in store.get_all(f"u:{EMAIL}:{TITLE}")),
                         {ITEM: {"modified": "2026-01-01T00:00:00Z", "new_page": True}})
        self.assertEqual(sorted(store.get_many(f"r:{EMAIL}:{TITLE}", [ITEM, f"{TITLE}/2", "none"])),
                         [ITEM, f"{TITLE}/2"])
        self.assertEqual(self.sql("SELECT item_title, resolved, significance FROM app.alert_state "
                                  "ORDER BY item_title"),
                         [(ITEM, False, "new_page"), (f"{TITLE}/2", True, None)])
        self.assertEqual(store.get_all("active"), [(f"{EMAIL}:{TITLE}", json.dumps({"email": EMAIL, "title": TITLE}))])

        stores = dict(self.sql("SELECT store, count(*) FROM app.kv GROUP BY store"))
        self.assertEqual(stores, {"bd_watchers_copied": 7, "bd_other": 1})

    def test_existing_state_wins_and_copy_runs_once_per_process(self):
        self.watch()
        store = AlertStateStore()
        store.insert(f"u:{EMAIL}:{TITLE}", ITEM, json.dumps({"modified": "new"}))   # copies (nothing)
        # rows written by an old process after the copy
        self.kv(f"u:{EMAIL}:{TITLE}", ITEM, {"modified": "old"})
        self.kv(f"u:{EMAIL}:{TITLE}", f"{TITLE}/2", {"modified": "late"})
        self.assertEqual(len(store.get_all(f"u:{EMAIL}:{TITLE}")), 1)   # this process has copied

        fresh = AlertStateStore()   # the next process picks them up
        self.assertEqual(dict((k, json.loads(v)) for k, v in fresh.get_all(f"u:{EMAIL}:{TITLE}")),
                         {ITEM: {"modified": "new"}, f"{TITLE}/2": {"modified": "late"}})
        self.assertEqual(self.sql("SELECT count(*) FROM app.kv WHERE store = 'bd_watchers'")[0][0], 0)

    def test_copy_is_transactional(self):
        self.watch()
        self.kv(f"u:{EMAIL}:{TITLE}", ITEM, {"modified": "x"})
        real_copy = alert_store.copy_kv_state

        def copy_then_fail(cur, store):
            real_copy(cur, store)
            raise RuntimeError("killed after the copy")

        with mock.patch.object(alert_store, "copy_kv_state", copy_then_fail):
            with self.assertRaises(RuntimeError):
                AlertStateStore().get_all(f"u:{EMAIL}:{TITLE}")
        self.assertEqual(self.sql("SELECT count(*) FROM app.alert_state")[0][0], 0)
        self.assertEqual(self.sql("SELECT count(*) FROM app.kv WHERE store = 'bd_watchers'")[0][0], 1)


class TestMigration016Grants(_DatabaseTest):
    def test_search_granted_to_web_role(self):
        if not self.sql("SELECT 1 FROM pg_roles WHERE rolname = 'birddog_web'"):
            self.skipTest("role birddog_web does not exist here (016 is a no-op)")
        acl = self.sql("SELECT proacl::text FROM pg_proc WHERE oid = 'core.search(text, int)'::regprocedure")
        self.assertIn("birddog_web=X", acl[0][0])

    def test_web_role_can_use_alert_state(self):
        if not self.sql("SELECT 1 FROM pg_roles WHERE rolname = 'birddog_web'"):
            self.skipTest("role birddog_web does not exist here")
        import psycopg2
        conn = psycopg2.connect(os.environ["BIRDDOG_DATABASE_URL"])
        try:
            with conn.cursor() as cur:
                cur.execute("SET LOCAL ROLE birddog_web")
                cur.execute("INSERT INTO app.user_account (email, name, password_hash) VALUES (%s, 'n', 'x')",
                            (EMAIL,))
                cur.execute("INSERT INTO app.watch (email, archive_title, watcher) VALUES (%s, %s, '{}')",
                            (EMAIL, TITLE))
                cur.execute("""INSERT INTO app.alert_state (email, archive_title, item_title, entry)
                               VALUES (%s, %s, %s, '{"modified": "2026-01-01T00:00:00Z"}')
                               RETURNING modified IS NOT NULL""", (EMAIL, TITLE, ITEM))
                self.assertTrue(cur.fetchone()[0])   # the trigger ran
                cur.execute("SELECT count(*) FROM core.search('x', 1)")
        finally:
            conn.rollback()
            conn.close()


if __name__ == "__main__":
    unittest.main()
