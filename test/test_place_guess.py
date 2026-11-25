"""Pure-logic tests for birddog/place_guess.py (no network, no database)."""

import json
import unittest
from pathlib import Path

import yaml

from birddog import place_guess as pgs

PLACES = [
    {"key": "канів|", "as_written": "Каневскаго", "normalized": "Канів", "admin_context": "",
     "hint": "1848 год. Метрическая книга синагоги Богуслава.pdf"},
    {"key": "безпечна|киевская губерния", "as_written": "Безпечной", "normalized": "Безпечна",
     "admin_context": "Киевская губерния", "hint": ""},
    {"key": "невідомо|", "as_written": "?", "normalized": "Невідомо", "admin_context": "", "hint": ""},
    {"key": "x|", "as_written": "x", "normalized": "x", "admin_context": "", "hint": ""},
]


class SchemaTests(unittest.TestCase):
    def test_schema_is_strict(self):
        item = pgs.GUESS_SCHEMA["properties"]["places"]["items"]
        self.assertIs(item["additionalProperties"], False)
        self.assertEqual(set(item["required"]), set(item["properties"]))

    def test_prompt_lines(self):
        lines = [json.loads(x) for x in pgs.prompt_lines(PLACES[:2]).splitlines()]
        self.assertEqual([x["id"] for x in lines], [0, 1])
        self.assertEqual(lines[1]["admin"], "Киевская губерния")
        self.assertIn("Богуслава", lines[0]["book"])


class ParseTests(unittest.TestCase):
    def test_answers(self):
        parsed = {"places": [
            {"id": 0, "found": True, "latitude": 49.75, "longitude": 31.46, "confidence": "high",
             "basis": "Kaniv, Kyiv oblast"},
            {"id": 1, "found": True, "latitude": 49.3, "longitude": 30.9, "confidence": "sure",
             "basis": "Bezpechna near Lysianka"},
            {"id": 2, "found": False, "latitude": 0, "longitude": 0, "confidence": "low",
             "basis": "means 'unknown'"},
            {"id": 0, "found": True, "latitude": 1, "longitude": 1, "confidence": "low", "basis": "dup"},
            {"id": 9, "found": True, "latitude": 1, "longitude": 1, "confidence": "low", "basis": "stray"},
        ]}
        out = pgs.parse_answers(PLACES, parsed)
        self.assertEqual(len(out), 4)
        self.assertEqual(out[0][1:4], (49.75, 31.46, "high"))       # first answer for an id wins
        self.assertEqual(out[1][3], "low")                           # unknown confidence -> low
        self.assertEqual(out[2][1:4], (None, None, "none"))          # not found
        self.assertEqual(out[2][4], "means 'unknown'")
        self.assertEqual(out[3][1:5], (None, None, "none", "not returned"))

    def test_out_of_range_is_no_answer(self):
        parsed = {"places": [{"id": 0, "found": True, "latitude": 123, "longitude": 30,
                              "confidence": "high", "basis": "b"}]}
        self.assertEqual(pgs.parse_answers(PLACES[:1], parsed)[0][3], "none")

    def test_junk(self):
        self.assertEqual([a[3] for a in pgs.parse_answers(PLACES[:2], None)], ["none", "none"])


class DagTests(unittest.TestCase):
    def test_guess_tasks(self):
        spec = yaml.safe_load((Path(__file__).resolve().parent.parent / "dags/yaml/books.yml").read_text())
        from birddog import dag_tasks_books
        for dag_id, after in (("read_books", "assemble"), ("transcribe_books", "index"),
                              ("guess_unmatched_places", None)):
            tasks = {t["task_id"]: t for t in spec[dag_id]["tasks"]}
            task = tasks["guess_places"]
            self.assertEqual(task["pool"], "llm_api")
            self.assertTrue(callable(getattr(dag_tasks_books, task["python_callable"].rsplit(".", 1)[1])))
            if after:
                self.assertEqual(task["dependencies"], [after])


if __name__ == "__main__":
    unittest.main()
