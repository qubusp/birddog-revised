"""core.repair_mojibake (migration 022) and the NocoDB location transform that
uses it. Needs a scratch database (BIRDDOG_TEST_DATABASE_URL, see
test/pg_scratch.py); skipped without one."""

import json
import unittest
from pathlib import Path

from test.pg_scratch import require_test_database

TRANSFORM = Path(__file__).resolve().parent.parent / "dags" / "sources" / "nocodb_locations" / "transform.sql"

CASES = [
    ("Korsunâ€™-Shevchenkivsâ€™kyy", "Korsun’-Shevchenkivs’kyy"),
    ("Ã“sÃ¡ndorfalva [Hun]", "Ósándorfalva [Hun]"),
    ("RjapiÄ\u008f [Slov]", "Rjapiď [Slov]"),     # byte 0x8F, undefined in Windows-1252
    ("Zürich", "Zürich"),                          # correct Latin-1 text: not UTF-8 underneath
    ("Łuck", "Łuck"),                              # not Windows-1252 at all
    ("WoÅ‚oska WieÅ›, Wołoska Wieś", "WoÅ‚oska WieÅ›, Wołoska Wieś"),  # mixed: left alone
    ("Plain ASCII", "Plain ASCII"),
    (None, None),
]


class RepairMojibakeTests(unittest.TestCase):
    def setUp(self):
        import psycopg2
        self.conn = psycopg2.connect(require_test_database())
        self.addCleanup(self.conn.close)

    def test_repair(self):
        with self.conn, self.conn.cursor() as cur:
            for broken, clean in CASES:
                cur.execute("SELECT core.repair_mojibake(%s)", (broken,))
                self.assertEqual(cur.fetchone()[0], clean, broken)
                cur.execute("SELECT core.repair_mojibake(core.repair_mojibake(%s))", (broken,))
                self.assertEqual(cur.fetchone()[0], clean, f"second pass {broken}")

    def test_source_transform_lands_clean_names(self):
        with self.conn.cursor() as cur:
            try:
                cur.execute("CREATE TEMP TABLE landed (key text, payload jsonb)")
                cur.execute("INSERT INTO landed VALUES (%s, %s)", ("zz-enc-1", json.dumps({
                    "modern_location_name": "Korsunâ€™", "alternate_names": "Ã“sÃ¡ndorfalva [Hun]",
                    "c1930_district": "RÃ³wne", "c1900_province": "Zürich"})))
                cur.execute(TRANSFORM.read_text(encoding="utf8"))
                cur.execute("""SELECT modern_location_name, alternate_names, c1930_district, c1900_province
                               FROM core.location WHERE location_id = 'zz-enc-1'""")
                self.assertEqual(cur.fetchone(), ("Korsun’", "Ósándorfalva [Hun]", "Równe", "Zürich"))
            finally:
                self.conn.rollback()


if __name__ == "__main__":
    unittest.main()
