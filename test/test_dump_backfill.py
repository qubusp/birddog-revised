import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from birddog import dump_backfill


def _touch(directory, name):
    with open(os.path.join(directory, name), "w") as f:
        f.write("x")


class TestPruneOldDumps(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        patcher = patch.object(dump_backfill, "DUMP_DIR", self.dir.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.dir.cleanup)

    def test_deletes_only_older_dumps(self):
        for name in ("ukwikisource-20260901-pages-articles.xml.bz2",
                     "ukwikisource-20261001-pages-articles.xml.bz2.part",
                     "ukwikisource-20261101-pages-articles.xml.bz2",
                     "ukwikisource-20261201-pages-articles.xml.bz2",
                     "notes.txt"):
            _touch(self.dir.name, name)
        deleted = dump_backfill.prune_old_dumps("20261101")
        self.assertEqual(deleted, ["ukwikisource-20260901-pages-articles.xml.bz2",
                                   "ukwikisource-20261001-pages-articles.xml.bz2.part"])
        self.assertEqual(sorted(os.listdir(self.dir.name)),
                         ["notes.txt",
                          "ukwikisource-20261101-pages-articles.xml.bz2",
                          "ukwikisource-20261201-pages-articles.xml.bz2"])

    def test_missing_directory(self):
        with patch.object(dump_backfill, "DUMP_DIR", os.path.join(self.dir.name, "nope")):
            self.assertEqual(dump_backfill.prune_old_dumps("20261101"), [])


class TestLoadPrunes(unittest.TestCase):

    def _run_load(self, conn):
        ctx = MagicMock()
        ctx.__enter__.return_value = conn
        with patch.object(dump_backfill, "download", return_value="/x.bz2"), \
             patch.object(dump_backfill, "_archive_pages", return_value=iter(())), \
             patch.object(dump_backfill.pg, "connect", return_value=ctx), \
             patch.object(dump_backfill, "set_watermark"):
            return dump_backfill.load("20261101")

    def test_prunes_after_successful_load(self):
        conn = MagicMock()
        conn.cursor.return_value.fetchone.return_value = None
        with patch.object(dump_backfill, "prune_old_dumps", return_value=["old"]) as prune:
            summary = self._run_load(conn)
        prune.assert_called_once_with("20261101")
        self.assertEqual(summary["deleted_dumps"], ["old"])

    def test_keeps_dumps_when_load_fails(self):
        conn = MagicMock()
        conn.cursor.return_value.fetchone.return_value = None
        conn.commit.side_effect = RuntimeError("database gone")
        with patch.object(dump_backfill, "prune_old_dumps") as prune:
            with self.assertRaises(RuntimeError):
                self._run_load(conn)
            prune.assert_not_called()


if __name__ == "__main__":
    unittest.main()
