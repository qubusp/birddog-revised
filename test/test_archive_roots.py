import os
import unittest
from unittest import mock

from birddog.archive_roots import PostgresArchiveRootStore

_TEST_DB = os.environ.get("BIRDDOG_TEST_DATABASE_URL")


@unittest.skipUnless(_TEST_DB, "BIRDDOG_TEST_DATABASE_URL not set")
class TestPostgresArchiveRootStore(unittest.TestCase):
    """Round trip against a real birddog database (core.archive_root). Uses
    clearly-fake titles and deletes them afterwards."""

    _TITLE = "Архів:__TestPgProbe__"

    def setUp(self):
        self._env = mock.patch.dict(os.environ, {"BIRDDOG_DATABASE_URL": _TEST_DB})
        self._env.start()
        self.store = PostgresArchiveRootStore()
        self.store.remove(self._TITLE)

    def tearDown(self):
        self.store.remove(self._TITLE)
        self._env.stop()

    def test_put_all_remove(self):
        self.store.put(self._TITLE, {"label": "Probe", "description": ""})
        self.assertEqual(self.store.all()[self._TITLE], {"label": "Probe", "description": ""})

        self.store.put(self._TITLE, {"label": "Probe2", "description": "d"})
        self.assertEqual(self.store.all()[self._TITLE], {"label": "Probe2", "description": "d"})

        self.store.remove(self._TITLE)
        self.assertNotIn(self._TITLE, self.store.all())

    def test_wiki_registry_uses_postgres(self):
        import birddog.wiki as wiki_mod
        with mock.patch.object(wiki_mod, "_archive_roots_store", self.store), \
             mock.patch.object(wiki_mod, "_archive_roots_cache", None):
            self.assertEqual(wiki_mod.register_archive_root(self._TITLE, label="PgProbe"), "PgProbe")
        self.assertEqual(self.store.all()[self._TITLE]["label"], "PgProbe")


class TestDefaultStore(unittest.TestCase):
    def test_selects_postgres_only_when_configured(self):
        from birddog import archive_roots
        with mock.patch.dict(os.environ, {"BIRDDOG_DATABASE_URL": "postgresql://x/y"}):
            self.assertIsInstance(archive_roots.default_store(), PostgresArchiveRootStore)
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch("birddog.store.KeyValueStore") as kv:
            self.assertIsInstance(archive_roots.default_store(), archive_roots.KVArchiveRootStore)
            kv.assert_called_once()


if __name__ == "__main__":
    unittest.main()
