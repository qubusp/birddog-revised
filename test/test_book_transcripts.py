"""Pure-logic tests for birddog/book_transcripts.py (no network, no database)."""

import unittest
from pathlib import Path

import yaml

from birddog import book_transcripts as bt
from birddog import books

SONNET, OPUS = books.READ_MODEL, books.REREAD_MODEL

ENTRY = {
    "entry_no": "12", "event": "birth", "date_text": "3 мая 1848", "date_iso": "1848-05-03",
    "person": "Лейба", "father": "Хаимъ Фрайштейнъ", "mother": "Ривка",
    "others": [{"name": "Мовша", "role": "mohel"}, {"name": "", "role": "witness"}],
    "places": [{"as_written": "Каневскаго", "normalized": "Канів", "name_en": "Kaniv",
                "role": "origin_or_registration"},
               {"as_written": "", "normalized": "", "name_en": "", "role": "other"}],
    "note": "",
}


class SchemaTests(unittest.TestCase):
    def _check(self, node, path="$"):
        """Structured outputs: every object closed, every property required."""
        if node.get("type") == "object":
            self.assertIs(node.get("additionalProperties"), False, path)
            self.assertEqual(set(node["required"]), set(node["properties"]), path)
            for name, child in node["properties"].items():
                self._check(child, f"{path}.{name}")
        if node.get("type") == "array":
            self._check(node["items"], f"{path}[]")

    def test_schema_is_strict(self):
        self._check(bt.TRANSCRIBE_SCHEMA)

    def test_places_match_read_books(self):
        page = bt.TRANSCRIBE_SCHEMA["properties"]["pages"]["items"]["properties"]
        self.assertEqual(page["places"],
                         books.READ_SCHEMA["properties"]["pages"]["items"]["properties"]["places"])


class DateTests(unittest.TestCase):
    def test_clean_iso(self):
        self.assertEqual(bt.clean_iso("1848-05-03"), "1848-05-03")
        self.assertEqual(bt.clean_iso("1848-05"), "1848-05")
        self.assertEqual(bt.clean_iso("1848"), "1848")
        self.assertIsNone(bt.clean_iso("1848-13-01"))
        self.assertIsNone(bt.clean_iso("03.05.1848"))
        self.assertIsNone(bt.clean_iso("2848"))
        self.assertIsNone(bt.clean_iso(""))
        self.assertIsNone(bt.clean_iso(None))

    def test_year(self):
        self.assertEqual(bt.entry_year("1848-05-03", ""), 1848)
        self.assertEqual(bt.entry_year("", "3 мая 1848 г."), 1848)
        self.assertEqual(bt.entry_year("", "1848, 1848"), 1848)
        self.assertIsNone(bt.entry_year("", "3 мая 1848, рожд. 1847"))   # two years: not certain
        self.assertIsNone(bt.entry_year("", "№ 12345"))
        self.assertIsNone(bt.entry_year("", "третьяго мая"))


class FlattenTests(unittest.TestCase):
    def test_best_reading_wins(self):
        pages = {3: {SONNET: {"entries": [ENTRY, ENTRY]}, OPUS: {"entries": [ENTRY]}},
                 4: {SONNET: {"entries": [dict(ENTRY, event="funeral", date_iso="")]}}}
        rows = bt.flatten_entries(pages)
        self.assertEqual([(r["page_no"], r["entry_idx"], r["model"]) for r in rows],
                         [(3, 1, OPUS), (4, 1, SONNET)])
        self.assertEqual(rows[1]["event"], "other")          # not in the enum
        self.assertEqual(rows[1]["year"], 1848)               # from date_text

    def test_entry_fields(self):
        row = bt.flatten_entries({3: {SONNET: {"entries": [ENTRY]}}})[0]
        self.assertEqual(row["entry_no"], "12")
        self.assertEqual(row["date_iso"], "1848-05-03")
        self.assertIsNone(row["note"])                        # empty -> null
        self.assertEqual(row["others"], [{"name": "Мовша", "role": "mohel"}])
        self.assertEqual(len(row["places"]), 1)               # the empty place is dropped

    def test_tolerates_junk(self):
        pages = {1: {SONNET: {"entries": None}}, 2: {SONNET: {"entries": ["x", {"places": None}]}}}
        rows = bt.flatten_entries(pages)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event"], "other")
        self.assertEqual(rows[0]["places"], [])

    def test_match_entry_places(self):
        rows = bt.flatten_entries({3: {SONNET: {"entries": [ENTRY]}}})
        names = [("kaniv", "-1040899", "Kaniv"), ("korsun", "-1", "Korsun")]
        bt.match_entry_places(rows, names)
        place = rows[0]["places"][0]
        self.assertEqual(place["location_id"], "-1040899")
        self.assertGreater(place["match_score"], 0.9)


class RereadRuleTests(unittest.TestCase):
    def test_only_illegible_pages(self):
        self.assertTrue(books._hard_page({"illegible": True, "entries": []}))
        self.assertFalse(books._hard_page({"illegible": False, "entries": [ENTRY]}))


def _entry(person):
    return dict(ENTRY, person=person)


class NamesRuleTests(unittest.TestCase):
    def test_unreadable_name(self):
        for person in ("", "   ", "[...]", "[…]", "[...] [...]", " [...]  ", "?", "[...] ?", None, 5):
            self.assertTrue(bt.unreadable_name(person), repr(person))
        for person in ("Сура?", "Аврум Бей?[...]", "[...] Мордка", "חיה [...] בת ישראל", "Лейба"):
            self.assertFalse(bt.unreadable_name(person), person)

    def test_threshold(self):
        ok, bad = _entry("Лейба"), _entry("[...]")
        self.assertFalse(bt.names_unreadable({"entries": []}))
        self.assertFalse(bt.names_unreadable({"entries": [ok, ok, ok, bad]}))     # 1 of 4
        self.assertTrue(bt.names_unreadable({"entries": [ok, ok, ok, bad, bad]}))  # 2 entries
        self.assertTrue(bt.names_unreadable({"entries": [bad]}))                   # 1 of 1
        self.assertTrue(bt.names_unreadable({"entries": [ok, bad]}))               # half
        self.assertTrue(bt.needs_reread({"illegible": True, "entries": []}))

    def test_reread_pages(self):
        ok, bad = _entry("Лейба"), _entry("")
        pages = {
            1: {SONNET: {"entries": [ok]}},                                       # fine
            2: {SONNET: {"entries": [bad, bad]}},                                 # names
            3: {SONNET: {"illegible": True, "entries": []}},                      # illegible
            4: {SONNET: {"entries": [bad]}, OPUS: {"entries": [bad], "prompt": None}},   # old Opus read
            5: {SONNET: {"entries": [bad]}, OPUS: {"entries": [bad], "prompt": "names"}},
            6: {SONNET: {"entries": [bad]}, OPUS: {"entries": [ok], "prompt": None}},    # Opus fixed it
        }
        self.assertEqual(bt.reread_pages(pages), [2, 3])
        self.assertEqual(bt.reread_pages(pages, rerun=True), [2, 3, 4])

    def test_names_prompt(self):
        self.assertIn("BEST reading", bt.NAMES_PROMPT)
        self.assertIn("Hebrew or Yiddish", bt.NAMES_PROMPT)

    def test_enqueue_task_modes(self):
        from unittest import mock
        from birddog import dag_tasks_books
        with mock.patch.object(bt, "enqueue_names_reread", return_value=6) as rr, \
             mock.patch.object(bt, "enqueue", return_value=0) as normal:
            self.assertEqual(dag_tasks_books.enqueue_transcripts("0", "2581, 2575;2580", "True"), 6)
            rr.assert_called_once_with([2581, 2575, 2580])
            normal.assert_not_called()
            dag_tasks_books.enqueue_transcripts("3", "", "False")
            normal.assert_called_once_with(3, [])


class DagTests(unittest.TestCase):
    def test_transcribe_dag_definition(self):
        spec = yaml.safe_load((Path(__file__).resolve().parent.parent / "dags/yaml/books.yml").read_text())
        dag = spec["transcribe_books"]
        self.assertEqual(dag["params"]["budget_usd"], 60)
        self.assertIs(dag["params"]["reread_names"], False)
        tasks = {t["task_id"]: t for t in dag["tasks"]}
        self.assertEqual(set(tasks), {"enqueue", "transcribe_submit", "transcribe_wait", "transcribe_collect",
                                      "reread_submit", "reread_wait", "reread_collect", "transcribe_finish",
                                      "index", "guess_places"})
        self.assertEqual(tasks["transcribe_submit"]["pool"], "llm_api")
        from birddog import dag_tasks_books
        for t in tasks.values():
            module, name = t["python_callable"].rsplit(".", 1)
            self.assertEqual(module, "birddog.dag_tasks_books")
            self.assertTrue(callable(getattr(dag_tasks_books, name)))


if __name__ == "__main__":
    unittest.main()
