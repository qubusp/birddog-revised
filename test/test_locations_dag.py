"""The location finder DAG (dags/yaml/locations.yml): birddog/dag_tasks_locations.py
and birddog/locations/pg_store.py. The LLM is always mocked (fake_extractor replaces the HTTP call);
no test calls a real provider.

The key guard tests need nothing. The rest need a scratch database
(BIRDDOG_TEST_DATABASE_URL, see test/pg_scratch.py) and are skipped without one.
They run the real FileLocationFinder and LocationMatcher over a few
synthetic core.location rows, under fake titles and ids, removed afterwards."""

import os
import unittest
from unittest.mock import patch

from test.pg_scratch import require_test_database

KEY = "GROQ_LOCATION_KEY"

# synthetic locations; -1060903 is Zhitomir's id in finder.py's tables
LOCATIONS = [
    ("-1060903", "Zhitomir", "Zhytomyr, Jitomir", "Zhitomir", "Volhynia"),
    ("-9100001", "Testovka", "Testiwka", "Novograd Volynskiy", "Volhynia"),
    ("-9100002", "Probnoye", None, "Ovruch", "Volhynia"),
]
TITLE = "zz_locations_test"


def fake_extractor(script, raise_exc=None):
    """A real LocationExtractor whose HTTP call (_create_completion) is
    replaced: it answers in the OpenAI chat-completion shape, giving each
    document the places scripted for the first key found in its
    descriptors. A None entry leaves the document out of the reply (the
    extractor then reports it failed); raise_exc makes every call raise."""
    import json
    import re
    from birddog.locations.extractor import LocationExtractor
    from birddog.log import get_logger

    class FakeExtractor(LocationExtractor):
        def __init__(self):
            super().__init__(get_logger(), provider="groq")
            self.calls = []

        def _create_completion(self, messages, max_tokens, num_docs):
            user = messages[-1]["content"]
            self.calls.append(user)
            if raise_exc:
                raise raise_exc
            blocks = re.split(r"^Document (\d+):$", user, flags=re.M)[1:]
            items = []
            for idx, body in zip(blocks[0::2], blocks[1::2]):
                places = []
                for needle, scripted in script.items():
                    if needle in body:
                        places = scripted
                        break
                if places is not None:
                    items.append({"document_index": int(idx), "locations": places})
            content = json.dumps({"extracted_locations": items})
            return {"choices": [{"message": {"content": content}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": len(user) // 4, "completion_tokens": len(content) // 4}}

    return FakeExtractor()


class KeyGuardTests(unittest.TestCase):
    def test_batches_skip_without_key(self):
        from birddog import dag_tasks_locations as t
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(KEY, None)
            with self.assertRaises(t.AirflowSkipException) as ctx:
                t.pending_location_batches()
        self.assertIn(KEY, str(ctx.exception))

    def test_locate_skips_without_key(self):
        from birddog import dag_tasks_locations as t
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(KEY, None)
            with self.assertRaises(t.AirflowSkipException):
                t.locate_batch([1, 2])

    def test_last_attempt_outside_airflow(self):
        from birddog import dag_tasks_locations as t
        self.assertTrue(t._last_attempt())

    def test_int_param(self):
        from birddog import dag_tasks_locations as t
        self.assertEqual(t._int("", 300), 300)
        self.assertEqual(t._int("25", 300), 25)
        self.assertEqual(t._int("x", 300), 300)


class PgLocationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        require_test_database()
        from birddog import pg
        cls.pg = pg

    def setUp(self):
        self._cleanup()
        with self.pg.connect() as conn, conn.cursor() as cur:
            for loc_id, name, alt, district, province in LOCATIONS:
                cur.execute("""INSERT INTO core.location (location_id, modern_location_name, alternate_names,
                                   c1900_district, c1900_province) VALUES (%s, %s, %s, %s, %s)
                               ON CONFLICT (location_id) DO NOTHING""",
                            (loc_id, name, alt, district, province))
            cur.execute("""INSERT INTO core.page (url, title, root_label, description_en, stale)
                           VALUES ('https://example.test/zz/archive', %s, 'DAZHO', NULL, false) RETURNING id""",
                        (TITLE + " archive",))
            archive = cur.fetchone()[0]
            cur.execute("""INSERT INTO core.page (url, title, root_label, description_en, parent_id, stale)
                           VALUES ('https://example.test/zz/opus', %s, 'DAZHO',
                                   'Metric books of the synagogue', %s, false) RETURNING id""",
                        (TITLE + " opus", archive))
            self.opus = cur.fetchone()[0]
            self.docs = {}
            # "none" has no owning page, so no archive to fall back on
            for key, desc in [("found", "Births in Testovka village, 1890"),
                              ("none", "An index without places"),
                              ("fail", "This one breaks the model"),
                              ("done", "Births in Testovka village, 1891"),
                              ("archive", "A list without places")]:
                cur.execute("""INSERT INTO core.document (url, title, description, location_status)
                               VALUES (%s, %s, %s, %s) RETURNING id""",
                            (f"https://example.test/zz/{key}.pdf", f"File:{TITLE}_{key}.pdf", desc,
                             "found" if key == "done" else "pending"))
                self.docs[key] = cur.fetchone()[0]
                if key != "none":
                    cur.execute("INSERT INTO core.page_document (page_id, document_id) VALUES (%s, %s)",
                                (self.opus, self.docs[key]))

    def tearDown(self):
        self._cleanup()

    def _cleanup(self):
        with self.pg.connect() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM core.document WHERE url LIKE 'https://example.test/zz/%'")
            cur.execute("DELETE FROM core.page WHERE url LIKE 'https://example.test/zz/%'")
            cur.execute("DELETE FROM core.location WHERE location_id = ANY(%s)",
                        ([loc[0] for loc in LOCATIONS],))

    def _status(self, key):
        with self.pg.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT location_status FROM core.document WHERE id = %s", (self.docs[key],))
            return cur.fetchone()[0]

    def _links(self, key):
        with self.pg.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT location_id FROM core.document_location WHERE document_id = %s ORDER BY 1",
                        (self.docs[key],))
            return [r[0] for r in cur.fetchall()]

    def _run(self, extractor, ids=None, **kwargs):
        from birddog.locations import pg_store
        records = pg_store.PgRecords()
        translator = pg_store.CachedTranslator()
        finder = pg_store.make_finder(records, translator, extractor=extractor)
        try:
            return pg_store.locate_documents(ids or [self.docs[k] for k in ("found", "none", "fail", "archive")],
                                             finder=finder, translator=translator, **kwargs)
        finally:
            records.close()

    # --- reads ---------------------------------------------------------------

    def test_records_have_nocodb_shape(self):
        from birddog.locations.pg_store import PgRecords
        records = PgRecords()
        try:
            doc = records.read("Documents", self.docs["found"])
            self.assertEqual(doc["description"], "Births in Testovka village, 1890")
            self.assertEqual(doc["page_description"], "Metric books of the synagogue")
            self.assertEqual(doc["owning_pages"], [{"Id": self.opus, "title": TITLE + " opus"}])
            page = records.read("Pages", self.opus)
            self.assertEqual(page["root_label"], "DAZHO")
            self.assertEqual(page["description"], "Metric books of the synagogue")
            parent = records.read("Pages", page["parent"][0]["Id"])
            self.assertIsNone(parent["parent"])
            self.assertEqual(records.read("Documents", -1), {})
            locs = {r["location_id"]: r for r in records.scan_all("Locations")}
            self.assertEqual(locs["-9100001"]["alternate_names"], "Testiwka")
        finally:
            records.close()

    def test_pending_ids_oldest_first(self):
        from birddog.locations.pg_store import pending_document_ids
        ids = pending_document_ids(100000)
        mine = [i for i in ids if i in self.docs.values()]
        self.assertEqual(mine, sorted(self.docs[k] for k in ("found", "none", "fail", "archive")))

    # --- runs ----------------------------------------------------------------

    def test_found_none_failed(self):
        # a stale link on the found document is replaced
        with self.pg.connect() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO core.document_location VALUES (%s, '-9100002')", (self.docs["found"],))
        extractor = fake_extractor({"Testovka": ["Testovka"], "breaks": None, "without": []})
        summary = self._run(extractor)
        self.assertEqual(self._status("found"), "found")
        self.assertEqual(self._links("found"), ["-9100001"])
        self.assertEqual(self._status("none"), "none")
        self.assertEqual(self._status("fail"), "failed")
        self.assertEqual(self._links("none"), [])
        # no place found: falls back to the archive's city (DAZHO -> Zhitomir)
        self.assertEqual(self._links("archive"), ["-1060903"])
        self.assertEqual((summary["found"], summary["none"], summary["failed"]), (2, 1, 1))
        self.assertEqual((summary["links_added"], summary["links_removed"]), (2, 1))
        # the archive's region centre does not replace a settlement match, and
        # the descriptions reached the (mocked) model
        self.assertIn("Testovka", " ".join(extractor.calls))

    def test_not_pending_is_skipped(self):
        extractor = fake_extractor({"Testovka": ["Testovka"]})
        summary = self._run(extractor, ids=[self.docs["done"]])
        self.assertEqual(summary["skipped_not_pending"], 1)
        self.assertEqual(extractor.calls, [])
        self.assertEqual(self._links("done"), [])

    def test_rate_limit_leaves_rest_pending(self):
        from birddog.fetch import RateLimitedError

        extractor = fake_extractor({"Testovka": ["Testovka"]})
        answer = extractor._create_completion

        def limited_after_first(messages, max_tokens, num_docs):
            if extractor.calls:
                raise RateLimitedError("daily tokens", retry_after=3600)
            return answer(messages, max_tokens, num_docs)
        extractor._create_completion = limited_after_first
        summary = self._run(extractor, chunk_size=1)
        self.assertTrue(summary["rate_limited"])
        self.assertEqual(summary["left_pending"], 3)
        self.assertEqual(self._status("found"), "found")  # first chunk kept
        self.assertEqual(self._status("none"), "pending")
        self.assertEqual(self._status("fail"), "pending")

    def test_error_raises_until_last_attempt(self):
        import requests
        extractor = fake_extractor({}, raise_exc=requests.exceptions.HTTPError("503 provider down"))
        with self.assertRaises(requests.exceptions.HTTPError):
            self._run(extractor, last_attempt=False)
        self.assertEqual(self._status("found"), "pending")
        summary = self._run(extractor, last_attempt=True)
        self.assertEqual(summary["failed"], 4)
        self.assertEqual(summary["chunk_errors"], 1)
        self.assertEqual(self._status("found"), "failed")

    def test_unknown_location_id_marks_failed(self):
        from birddog.locations.pg_store import write_results
        summary = {"found": 0, "none": 0, "failed": 0, "links_added": 0, "links_removed": 0}
        write_results({self.docs["found"]: (False, ["-999999999"], [], [], set())}, summary)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(self._status("found"), "failed")

    def test_locate_batch_with_key(self):
        """The task callable end to end, LLM mocked at the extractor class."""
        from birddog import dag_tasks_locations as t
        fake = fake_extractor({"Testovka": ["Testovka"], "breaks": None})
        with patch.dict(os.environ, {KEY: "test-not-a-real-key"}), \
                patch("birddog.locations.finder.LocationExtractor", lambda *a, **k: fake), \
                patch("birddog.translate.translation", side_effect=AssertionError("no translation")):
            batches = t.pending_location_batches("100000")
            mine = [i for b in batches for i in b if i in self.docs.values()]
            self.assertEqual(len(mine), 4)
            summary = t.locate_batch([self.docs["found"], self.docs["none"]])
        self.assertEqual(summary["found"], 1)
        self.assertEqual(self._status("none"), "none")

    def test_translator_cache_and_disabled(self):
        import hashlib
        from birddog.locations.pg_store import CachedTranslator
        from birddog.translate import TranslationDisabledError
        text = "зз тестовий текст"
        key = hashlib.sha256(text.encode("utf-8")).hexdigest()
        tr = CachedTranslator()
        try:
            with patch("birddog.translate.translation", side_effect=TranslationDisabledError("off")):
                self.assertEqual(tr(text), "")
            self.assertEqual(tr.disabled, 1)
            with patch("birddog.translate.translation", return_value="zz test text"):
                self.assertEqual(tr(text), "zz test text")
            with patch("birddog.translate.translation", side_effect=AssertionError("cached")):
                self.assertEqual(tr(text), "zz test text")
            self.assertEqual((tr.translated, tr.cached), (1, 1))
        finally:
            with self.pg.connect() as conn, conn.cursor() as cur:
                cur.execute("DELETE FROM core.translation WHERE source_hash = %s", (key,))

    def test_app_refresh_requeues_documents(self):
        """app_refresh_page's update_locations puts documents back on the queue
        this DAG drains."""
        from birddog.dag_tasks_app import _queue_locations
        with self.pg.connect() as conn, conn.cursor() as cur:
            n = _queue_locations(cur, "https://example.test/zz/archive", deep=True)
        self.assertEqual(n, 4)  # all but "none", which has no page
        self.assertEqual(self._status("done"), "pending")


if __name__ == "__main__":
    unittest.main()
