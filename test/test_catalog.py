"""Tests for the Postgres read layer (birddog/catalog.py) and the routes that
use it (/page, /archives, /search, /translate).

The database tests need a throwaway database with deploy/db/migrations
applied, given as BIRDDOG_TEST_DATABASE_URL; they insert fixture rows under
clearly-fake titles and delete them afterwards."""

import os
import unittest
from datetime import datetime, timezone
from unittest import mock
from unittest.mock import MagicMock, patch

from itsdangerous import URLSafeTimedSerializer

from birddog import catalog

_TEST_DB = os.environ.get("BIRDDOG_TEST_DATABASE_URL")


# ----------------------------------------------------------------------------
# row building (no database)

class TestRowBuilding(unittest.TestCase):
    def test_iso(self):
        dt = datetime(2025, 10, 9, 10, 36, 46, tzinfo=timezone.utc)
        self.assertEqual(catalog._iso(dt), "2025-10-09T10:36:46Z")
        self.assertEqual(catalog._iso(None), "")

    def test_text_item(self):
        self.assertEqual(catalog._text_item("Опис 1", "Inventory 1"), {"uk": "Опис 1", "en": "Inventory 1"})
        # no English: left out so the frontend shows the Ukrainian
        self.assertEqual(catalog._text_item("Опис", None), {"uk": "Опис"})
        # numbers need no translation
        self.assertEqual(catalog._text_item("1846", None), {"uk": "1846", "en": "1846"})
        self.assertEqual(catalog._text_item(None, None), {"uk": "", "en": ""})

    def test_child_row_wiki_page(self):
        child = {"title": "ДАМО/Р-2641/1", "domain": "uk.wikisource.org", "url": "u",
                 "availability": "linked", "native_description": "Опис 1",
                 "description_en": None, "years": "1942–1943", "doc_count": 2}
        row = catalog._child_row(child, {"Опис 1": "Inventory 1"})
        self.assertEqual(row[0], {"text": {"uk": "1", "en": "1"},
                                  "link": "/wiki/Архів:ДАМО/Р-2641/1", "exists": True})
        self.assertEqual(row[1]["text"], {"uk": "Опис 1", "en": "Inventory 1"})
        self.assertEqual(row[2]["text"]["uk"], "1942–1943")
        self.assertEqual(row[3]["text"]["uk"], "2")

    def test_child_row_redlinked_and_external(self):
        red = catalog._child_row({"title": "X/1/2", "domain": "uk.wikisource.org", "url": "u",
                                  "availability": "redlinked"}, {})
        self.assertFalse(red[0]["exists"])
        ext = catalog._child_row({"title": "X/1/3", "domain": "kherson.archives.gov.ua",
                                  "url": "https://kherson.archives.gov.ua/x", "availability": "linked"}, {})
        # not openable in the browse view (frontend is_linked needs /wiki/ and exists)
        self.assertEqual(ext[0]["link"], "https://kherson.archives.gov.ua/x")
        self.assertFalse(ext[0]["exists"])

    def test_document_row(self):
        row = catalog._document_row({"url": "https://c/File:a.pdf", "title": "File:a.pdf",
                                     "description": "Birth book", "availability": "linked"})
        self.assertEqual(row[0]["text"], {"uk": "File:a.pdf", "en": "File:a.pdf"})
        self.assertTrue(row[0]["exists"])
        self.assertEqual(row[1]["text"]["en"], "Birth book")

    def test_page_kind(self):
        self.assertEqual(catalog.page_kind("Архів:ДАМО/Р-2641/1/5", False), "case")
        self.assertEqual(catalog.page_kind("Архів:ДАМО/Р-2641/1/5", True), "opus")
        self.assertEqual(catalog.page_kind("Архів:ДАМО/Р-2641/1", True), "opus")

    def test_search_empty_query(self):
        self.assertEqual(catalog.search("   "), [])


# ----------------------------------------------------------------------------
# against a database

_ROOT = "__AppDataTest__"


@unittest.skipUnless(_TEST_DB, "BIRDDOG_TEST_DATABASE_URL not set")
class TestCatalogDatabase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls._env = mock.patch.dict(os.environ, {"BIRDDOG_DATABASE_URL": _TEST_DB})
        cls._env.start()
        from birddog import pg
        cls.pg = pg
        cls._cleanup()
        with pg.connect() as conn, conn.cursor() as cur:
            def page(title, parent=None, level="fond", native="", en=None, years="",
                     availability="linked", domain="uk.wikisource.org", url=None,
                     ts="2025-01-02T03:04:05Z", stale=False):
                url = url or catalog.page_url(title)
                cur.execute(
                    """INSERT INTO core.page (url, title, domain, parent_id, label, level,
                                              native_description, description_en, years,
                                              wiki_timestamp, availability, source_type, stale,
                                              seq_label)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                       RETURNING id""",
                    (url, title, domain, parent, title, level, native, en, years, ts,
                     availability, "wiki" if domain == "uk.wikisource.org" else "archives",
                     stale, title))
                return cur.fetchone()[0]

            def doc(url, page_id, description=""):
                cur.execute(
                    """INSERT INTO core.document (url, title, domain, description, availability)
                       VALUES (%s, %s, 'commons.wikimedia.org', %s, 'linked') RETURNING id""",
                    (url, url.rsplit("/", 1)[-1], description))
                cur.execute("INSERT INTO core.page_document VALUES (%s, %s)", (page_id, cur.fetchone()[0]))

            cur.execute("INSERT INTO core.archive_root (title, label) VALUES (%s, 'APPDATATEST')",
                        (f"Архів:{_ROOT}",))
            root = page(_ROOT, level="archive", native="Тестовий архів", en="Test archive")
            fond = page(f"{_ROOT}/1", root, native="Фонд один", years="1800–1900")
            page(f"{_ROOT}/2", root, availability="redlinked", ts=None, stale=True)
            opus = page(f"{_ROOT}/1/1", fond, level="opus", native="Опис перекладений")
            case = page(f"{_ROOT}/1/1/1", opus, level="case", native="Метрична книга", years="1850")
            page(f"{_ROOT}/1/1/1", opus, level="case", domain="kherson.archives.gov.ua",
                 url="https://kherson.archives.gov.ua/__appdatatest__")
            doc("https://commons.wikimedia.org/wiki/File:__AppDataTest__a.pdf", case, "Birth book")
            doc("https://commons.wikimedia.org/wiki/File:__AppDataTest__b.pdf", case)
            # translation cache entry for the opus description
            cur.execute(
                """INSERT INTO core.translation (source_hash, source_text, translated_text)
                   VALUES (%s, %s, 'Translated inventory') ON CONFLICT DO NOTHING""",
                (catalog._source_hash("Опис перекладений"), "Опис перекладений"))

    @classmethod
    def tearDownClass(cls):
        cls._cleanup()
        cls._env.stop()

    @classmethod
    def _cleanup(cls):
        with cls.pg.connect() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM core.document WHERE url LIKE %s", ("%__AppDataTest__%",))
            cur.execute("DELETE FROM core.page WHERE title LIKE %s", (f"{_ROOT}%",))
            cur.execute("DELETE FROM core.archive_root WHERE title = %s", (f"Архів:{_ROOT}",))
            cur.execute("DELETE FROM core.translation WHERE source_hash = %s",
                        (catalog._source_hash("Опис перекладений"),))

    def setUp(self):
        # page labels come from the archive-root registry, in Postgres here
        import birddog.wiki as wiki
        patcher = mock.patch.multiple(wiki, _archive_roots_store=None, _archive_roots_cache=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_get_page_by_title_forms(self):
        for title in (f"Архів:{_ROOT}/1", f"{_ROOT}/1", f"Архіви/{_ROOT}/1", f"Архів:{_ROOT}/1/"):
            row = catalog.get_page(title)
            self.assertIsNotNone(row, title)
            self.assertEqual(row["title"], f"{_ROOT}/1")
        self.assertIsNone(catalog.get_page(f"{_ROOT}/nope"))

    def test_root_payload(self):
        p = catalog.page_payload(_ROOT)
        self.assertEqual(p["title"], f"Архів:{_ROOT}")
        self.assertEqual(p["name"], "APPDATATEST")
        self.assertEqual(p["kind"], "archive")
        self.assertEqual(p["description"], {"uk": "Тестовий архів", "en": "Test archive"})
        self.assertFalse(p["needs_translation"])
        self.assertEqual(p["lastmod"], "2025-01-02T03:04:05Z")
        self.assertEqual(p["doc_link"], "")
        self.assertEqual(p["source"], "database")
        [contents] = p["tables"]
        self.assertEqual(contents["name"], catalog.CONTENTS_TABLE)
        self.assertEqual(len(contents["header"]), 4)
        rows = contents["children"]
        self.assertEqual([r[0]["text"]["uk"] for r in rows], ["1", "2"])
        self.assertEqual(rows[0][0]["link"], f"/wiki/Архів:{_ROOT}/1")
        self.assertTrue(rows[0][0]["exists"])
        self.assertFalse(rows[1][0]["exists"])          # redlinked
        self.assertEqual(rows[0][1]["text"], {"uk": "Фонд один"})
        self.assertEqual(rows[0][2]["text"]["uk"], "1800–1900")

    def test_translation_cache_fills_missing_english(self):
        p = catalog.page_payload(f"{_ROOT}/1/1")
        self.assertEqual(p["description"]["en"], "Translated inventory")
        self.assertFalse(p["needs_translation"])
        fond = catalog.page_payload(f"{_ROOT}/1")
        self.assertTrue(fond["needs_translation"])
        self.assertEqual(fond["tables"][0]["children"][0][1]["text"]["en"], "Translated inventory")

    def test_opus_lists_wiki_and_external_children(self):
        rows = catalog.page_payload(f"{_ROOT}/1/1")["tables"][0]["children"]
        self.assertEqual(len(rows), 2)
        wiki_row = next(r for r in rows if r[0]["link"].startswith("/wiki/"))
        self.assertEqual(wiki_row[3]["text"]["uk"], "2")    # two documents
        external = next(r for r in rows if not r[0]["link"].startswith("/wiki/"))
        self.assertFalse(external[0]["exists"])

    def test_case_documents(self):
        p = catalog.page_payload(f"{_ROOT}/1/1/1")
        self.assertEqual(p["kind"], "case")
        self.assertTrue(p["doc_link"].endswith("__AppDataTest__a.pdf"))
        [docs] = p["tables"]
        self.assertEqual(docs["name"], catalog.DOCUMENTS_TABLE)
        self.assertEqual(len(docs["children"]), 2)
        self.assertEqual(docs["children"][0][1]["text"]["en"], "Birth book")

    def test_missing_page(self):
        self.assertIsNone(catalog.page_payload(f"{_ROOT}/9/9"))

    def test_archive_roots(self):
        roots = catalog.archive_roots()
        mine = [r for r in roots if r["title"] == f"Архів:{_ROOT}"]
        self.assertEqual(mine, [{"title": f"Архів:{_ROOT}", "label": "APPDATATEST", "description": "",
                                 "url": f"https://uk.wikisource.org/wiki/Архів:{_ROOT}"}])
        self.assertEqual([r["label"] for r in roots], sorted(r["label"] for r in roots))

    def test_search(self):
        hits = catalog.search("Метрична книга", limit=10)
        page_hits = [h for h in hits if h["kind"] == "page" and h["title"] == f"{_ROOT}/1/1/1"]
        self.assertTrue(page_hits)
        self.assertEqual(page_hits[0]["page_title"], f"Архів:{_ROOT}/1/1/1")
        doc_hits = [h for h in catalog.search("Birth book", limit=10) if h["kind"] == "document"]
        self.assertTrue(doc_hits)
        # a document opens on its owning wiki page
        self.assertEqual(doc_hits[0]["page_title"], f"Архів:{_ROOT}/1/1/1")

    def test_connection_is_read_only(self):
        import psycopg2
        with self.assertRaises(psycopg2.errors.ReadOnlySqlTransaction):
            with catalog._cursor() as cur:
                cur.execute("DELETE FROM core.archive_root WHERE false")

    def test_connections_are_pooled(self):
        # one connection per process, not one per call (catalog used to
        # reconnect on every read)
        with catalog._cursor() as cur:
            cur.execute("SELECT pg_backend_pid()")
            first = cur.fetchone()[0]
        catalog.search("Метрична книга", limit=1)
        catalog.page_payload(f"Архів:{_ROOT}")
        with catalog._cursor() as cur:
            cur.execute("SELECT pg_backend_pid()")
            self.assertEqual(cur.fetchone()[0], first)

    def test_pooled_connection_stays_read_only_after_an_error(self):
        import psycopg2
        for _ in range(2):
            with self.assertRaises(psycopg2.errors.ReadOnlySqlTransaction):
                with catalog._cursor() as cur:
                    cur.execute("INSERT INTO core.archive_root (title, label) VALUES ('x', 'x')")
        # the failed transaction was rolled back; the connection is usable
        with catalog._cursor() as cur:
            cur.execute("SHOW transaction_read_only")
            self.assertEqual(cur.fetchone()[0], "on")

    def test_each_call_is_one_transaction(self):
        # a page payload's queries share one snapshot
        with catalog._cursor() as cur:
            cur.execute("SELECT now()")
            first = cur.fetchone()[0]
            cur.execute("SELECT now()")
            self.assertEqual(cur.fetchone()[0], first)


class TestReadPool(unittest.TestCase):
    """app_db's read-only pool, without a database."""

    def setUp(self):
        from birddog import app_db
        self.app_db = app_db
        app_db.reset_pool()
        self.addCleanup(app_db.reset_pool)

    def _fake_connect(self):
        conns = []

        def connect(url):
            conn = MagicMock(closed=False)
            conn.url = url
            conns.append(conn)
            return conn
        return conns, connect

    def test_catalog_reads_borrow_a_read_only_pooled_connection(self):
        conns, connect = self._fake_connect()
        with mock.patch.dict(os.environ, {"BIRDDOG_DATABASE_URL": "postgresql://h/one"}), \
             patch("psycopg2.connect", side_effect=connect):
            for _ in range(3):
                with catalog._cursor():
                    pass
        self.assertEqual(len(conns), 1)
        conns[0].set_session.assert_called_once_with(readonly=True, autocommit=False)
        self.assertEqual(conns[0].commit.call_count, 3)

    def test_read_pool_is_separate_from_the_store_pool(self):
        conns, connect = self._fake_connect()
        with mock.patch.dict(os.environ, {"BIRDDOG_DATABASE_URL": "postgresql://h/one"}), \
             patch("psycopg2.connect", side_effect=connect):
            with self.app_db.read_cursor():
                pass
            with self.app_db.cursor():
                pass
        self.assertEqual(len(conns), 2)
        self.assertTrue(conns[1].autocommit)
        conns[1].set_session.assert_not_called()

    def test_new_database_url_gets_a_new_pool(self):
        conns, connect = self._fake_connect()
        with patch("psycopg2.connect", side_effect=connect):
            for url in ("postgresql://h/one", "postgresql://h/two"):
                with mock.patch.dict(os.environ, {"BIRDDOG_DATABASE_URL": url}):
                    with catalog._cursor():
                        pass
        self.assertEqual([c.url for c in conns], ["postgresql://h/one", "postgresql://h/two"])
        conns[0].close.assert_called_once()


# ----------------------------------------------------------------------------
# routes

class _User:
    email = "catalog_test@example.com"
    name = "Catalog Test"
    role = "user"

    def __init__(self):
        self.preferences = {}

    def set_preference(self, key, value):
        self.preferences[key] = value

    def get_preference(self, key, default=None):
        return self.preferences.get(key, default)


def _payload(title="Архів:ДАМО/Р-2641", lastmod="2025-01-02T03:04:05Z"):
    return {"title": title, "name": "DAMO/R-2641", "lastmod": lastmod, "tables": [],
            "description": {"uk": ""}, "dates": {"uk": ""}, "doc_link": "",
            "kind": "fond", "needs_translation": False, "source": "database"}


class _LivePage:
    """Stands in for core.Page (the live wiki path)."""
    def __init__(self, title, page=None):
        self.title = title
        self.page = page or {"lastmod": "2026-01-01T00:00:00Z"}
        self.kind = "fond"
        self.name = "X/1"
        self.needs_translation = False

    def history(self, cutoff_date=None):
        return []

    def compare(self, ref_date):
        return _LivePage(self.title, {**self.page, "refmod": ref_date})


class TestRoutes(unittest.TestCase):
    def setUp(self):
        import birddog.service as service
        self.service = service
        service.app.config["TESTING"] = True
        service.app.secret_key = "test_secret"
        service.serializer = URLSafeTimedSerializer(service.app.secret_key)
        self.user = _User()
        service.users = MagicMock()
        service.users.lookup.return_value = self.user
        service.runtime = MagicMock(state="running")
        self.client = service.app.test_client()
        with self.client.session_transaction() as sess:
            sess["user"] = {"email": self.user.email, "name": self.user.name}
        for target, value in (("birddog.catalog.enabled", True),
                              ("birddog.catalog.refresh_archive_root_labels", None)):
            patcher = patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_page_from_database(self):
        history = [{"modified": "2025-01-02T03:04:05Z", "link": "x?oldid=2"},
                   {"modified": "2024-01-01T00:00:00Z", "link": "x?oldid=1"}]
        with patch("birddog.catalog.page_payload", return_value=_payload()), \
             patch("birddog.service.page_history", return_value=history):
            resp = self.client.get("/page?title=Архів:ДАМО/Р-2641")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["source"], "database")
        self.assertEqual(len(data["history"]), 2)
        self.assertEqual(data["lineage"][0], "Архів:ДАМО/Р-2641")
        self.assertIn("lineage_labels", data)
        self.assertFalse(data["no_earlier_version"])
        self.service.runtime.lookup_by_title.assert_not_called()
        self.assertEqual(self.user.preferences["last_page"], "Архів:ДАМО/Р-2641")

    def test_page_falls_back_to_wiki_when_database_is_behind(self):
        self.service.runtime.lookup_by_title.return_value = _LivePage("Архів:ДАМО/Р-2641")
        history = [{"modified": "2026-01-01T00:00:00Z", "link": "x?oldid=3"}]
        with patch("birddog.catalog.page_payload", return_value=_payload()), \
             patch("birddog.service.page_history", return_value=history):
            resp = self.client.get("/page?title=Архів:ДАМО/Р-2641")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["source"], "wiki")
        self.service.runtime.lookup_by_title.assert_called_once()

    def test_page_falls_back_when_missing_or_db_error(self):
        self.service.runtime.lookup_by_title.return_value = _LivePage("Архів:X/1")
        for side_effect in (lambda t: None, RuntimeError("db down")):
            with patch("birddog.catalog.page_payload", side_effect=side_effect):
                resp = self.client.get("/page?title=Архів:X/1")
            self.assertEqual(resp.get_json()["source"], "wiki")

    def test_page_keeps_database_when_wiki_history_unavailable(self):
        with patch("birddog.catalog.page_payload", return_value=_payload()), \
             patch("birddog.service.page_history", side_effect=RuntimeError("wiki down")):
            data = self.client.get("/page?title=Архів:ДАМО/Р-2641").get_json()
        self.assertEqual(data["source"], "database")
        self.assertEqual(data["history"], [])

    def test_compare_uses_wiki(self):
        self.service.runtime.lookup_by_title.return_value = _LivePage("Архів:X/1")
        with patch("birddog.catalog.page_payload") as payload:
            resp = self.client.get("/page?title=Архів:X/1&compare=2024-01-01")
        payload.assert_not_called()
        self.assertEqual(resp.get_json()["source"], "wiki")

    def test_archives_from_database(self):
        roots = [{"title": "Архів:A", "label": "A", "description": "", "url": "u"}]
        with patch("birddog.catalog.archive_roots", return_value=roots):
            resp = self.client.get("/archives")
        self.assertEqual(resp.get_json(), roots)

    def test_search(self):
        self.assertEqual(self.client.get("/search").status_code, 400)
        hits = [{"kind": "page", "title": "ДАЗО/1/1/12", "page_title": "Архів:ДАЗО/1/1/12"}]
        with patch("birddog.catalog.search", return_value=hits) as search:
            resp = self.client.get("/search?q=ДАЗО 1-1-12&limit=5")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"query": "ДАЗО 1-1-12", "results": hits})
        search.assert_called_once_with("ДАЗО 1-1-12", limit=5)

    def _home(self):
        self.user.get_watchlist = lambda: {}
        self.service.runtime.database_update_enabled = False
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        return resp.get_data(as_text=True)

    def test_home_has_search_box_with_database(self):
        html = self._home()
        self.assertIn('id="nav-search-tab"', html)
        self.assertIn('<label for="search-input"', html)
        self.assertIn('role="search"', html)
        self.assertIn('id="search-status" class="mb-2" role="status" aria-live="polite"', html)

    def test_home_has_no_search_box_without_database(self):
        with patch("birddog.catalog.enabled", return_value=False):
            html = self._home()
        self.assertNotIn("nav-search-tab", html)
        self.assertNotIn("search-input", html)

    def test_home_has_no_search_box_when_logged_out(self):
        with self.client.session_transaction() as sess:
            sess.clear()
        html = self._home()
        self.assertNotIn("search-input", html)

    def test_search_without_database(self):
        with patch("birddog.catalog.enabled", return_value=False):
            self.assertEqual(self.client.get("/search?q=x").status_code, 503)

    def test_translate_goes_through_runtime_seam(self):
        self.service.runtime.active_translations = []
        self.service.runtime.translation_enabled = False
        self.service.runtime.translation_available = False
        resp = self.client.get("/translate?title=Архів:X/1")
        self.assertEqual(resp.status_code, 200)
        self.service.runtime.request_translation.assert_called_once_with("Архів:X/1")


# ----------------------------------------------------------------------------
# runtime in Postgres mode

class TestRuntimePostgresMode(unittest.TestCase):
    def make_runtime(self, jobs):
        from birddog.runtime import Runtime
        runtime = Runtime.__new__(Runtime)
        runtime._jobs = jobs
        runtime._translation_manager = MagicMock(enabled=True, available=True)
        return runtime

    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"BIRDDOG_DATABASE_URL": "postgresql://x/y"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_no_backend(self):
        from birddog.runtime import BackgroundJobs
        runtime = self.make_runtime(BackgroundJobs())
        self.assertTrue(runtime.reads_from_database)
        self.assertFalse(runtime.database_update_enabled)
        self.assertFalse(runtime.translation_enabled)
        self.assertIsNone(runtime.active_database_updates)
        self.assertFalse(runtime.request_translation("Архів:X/1"))
        runtime.update_to_database("Архів:X/1")   # no-op, no error

    def test_with_backend(self):
        jobs = MagicMock(available=True)
        jobs.status.return_value = {"refresh_1": {}}
        runtime = self.make_runtime(jobs)
        self.assertTrue(runtime.database_update_enabled)
        runtime.update_to_database("Архів:X/1", deep=True, update_locations=True)
        jobs.request_page_refresh.assert_called_once_with("Архів:X/1", deep=True, update_locations=True)
        self.assertTrue(runtime.request_translation("Архів:X/1"))
        jobs.request_translation.assert_called_once_with("Архів:X/1")
        self.assertEqual(runtime.active_database_updates, {"refresh_1": {}})
        runtime.cancel_update("refresh_1")
        jobs.cancel.assert_called_once_with("refresh_1")

    def test_legacy_sync_off_without_nocodb(self):
        from birddog.runtime import _legacy_sync_enabled
        with mock.patch.dict(os.environ, {"BIRDDOG_NOCODB_ENV": "LOCAL"}):
            self.assertFalse(_legacy_sync_enabled())      # Postgres wins
        with mock.patch.dict(os.environ, {"BIRDDOG_DATABASE_URL": "", "BIRDDOG_NOCODB_ENV": ""}):
            self.assertFalse(_legacy_sync_enabled())
        with mock.patch.dict(os.environ, {"BIRDDOG_DATABASE_URL": "", "BIRDDOG_NOCODB_ENV": "LOCAL"}):
            self.assertTrue(_legacy_sync_enabled())

if __name__ == "__main__":
    unittest.main()
