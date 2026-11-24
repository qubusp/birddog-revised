"""Pure-logic tests for birddog/books.py (no network, no database)."""

import unittest

from birddog import books


class SamplePagesTests(unittest.TestCase):
    def test_small_book_takes_every_page(self):
        self.assertEqual(books.sample_pages(2), [1, 2])

    def test_spread_and_skips_cover(self):
        pages = books.sample_pages(363)
        self.assertEqual(len(pages), 3)
        self.assertNotIn(1, pages)
        self.assertEqual(pages, sorted(set(pages)))
        self.assertTrue(all(2 <= p <= 363 for p in pages))

    def test_short_book_still_distinct(self):
        self.assertEqual(len(set(books.sample_pages(4))), 3)


class EstimateDecideTests(unittest.TestCase):
    def test_estimate_grows_with_pages(self):
        small, small_usd = books.estimate(50, 1600)
        big, big_usd = books.estimate(363, 1600)
        self.assertLess(small, big)
        self.assertLess(small_usd, big_usd)

    def test_decide(self):
        ok = {"readable": True, "legibility": 3}
        self.assertEqual(books.decide(ok, 100_000, 500_000), "approved")
        self.assertEqual(books.decide(ok, 600_000, 500_000), "needs_approval")
        self.assertEqual(books.decide({"readable": False, "legibility": 4}, 1, 500_000), "unreadable")
        self.assertEqual(books.decide({"readable": True, "legibility": 1}, 1, 500_000), "unreadable")


class AssembleTests(unittest.TestCase):
    PAGES = {
        3: {"places": [
            {"as_written": "Острожской", "normalized": "Острог", "admin_context": "Житомирской губ.",
             "kind": "origin_or_registration", "quote": "", "confidence": "high"},
            {"as_written": "Симферополь", "normalized": "Сімферополь", "admin_context": "",
             "kind": "event_place", "quote": "", "confidence": "high"}]},
        4: {"illegible": True, "places": []},
        5: {"places": [
            {"as_written": "Острожской", "normalized": "Острог", "admin_context": "Житомирской губ.",
             "kind": "origin_or_registration", "quote": "", "confidence": "medium"},
            {"as_written": "Острогъ", "normalized": "Острог", "admin_context": "",
             "kind": "residence", "quote": "", "confidence": "low"}]},
    }

    def test_collect_merges_identical_mentions(self):
        mentions = books.collect_mentions(self.PAGES)
        self.assertEqual(len(mentions), 3)
        ostrozhskoi = next(m for m in mentions if m["as_written"] == "Острожской")
        self.assertEqual(ostrozhskoi["pages"], [3, 5])
        self.assertEqual(ostrozhskoi["count"], 2)

    def test_groups_and_leftovers(self):
        mentions = books.collect_mentions(self.PAGES)
        ids = {m["as_written"]: m["id"] for m in mentions}
        groups = [{"name_uk": "Острог", "name_en": "Ostroh", "admin_context": "Volhynia",
                   "mention_ids": [ids["Острожской"], ids["Острогъ"], 999]}]
        places = books.group_mentions(mentions, groups)
        self.assertEqual(len(places), 2)          # Simferopol left out by the model, kept anyway
        ostroh = places[0]
        self.assertEqual(ostroh["name_en"], "Ostroh")
        self.assertEqual(ostroh["pages"], [3, 5])
        self.assertEqual(ostroh["mentions"], 3)
        self.assertEqual(set(ostroh["kinds"]), {"origin_or_registration", "residence"})

    def test_match_places(self):
        names = [("ostroh", -1, "Ostroh"), ("ostrog", -1, "Ostroh"), ("simferopol", -2, "Simferopol")]
        places = [{"name_uk": "Острог", "name_en": "Ostroh", "variants": []},
                  {"name_uk": "Невідоме", "name_en": "Nowhereville", "variants": []}]
        books.match_places(places, names)
        self.assertEqual(places[0]["location_id"], -1)
        self.assertIsNone(places[1]["location_id"])

    def test_best_reading_prefers_reread(self):
        readings = {books.READ_MODEL: {"a": 1}, books.REREAD_MODEL: {"b": 2}}
        self.assertEqual(books.best_reading(readings), {"b": 2})

    def test_hard_page(self):
        self.assertTrue(books._hard_page(self.PAGES[4]))
        self.assertTrue(books._hard_page(self.PAGES[5]))
        self.assertFalse(books._hard_page(self.PAGES[3]))


class FileTitleTests(unittest.TestCase):
    def test_file_title(self):
        url = "https://commons.wikimedia.org/wiki/File:%D0%94%D0%90%D0%90%D0%A0%D0%9A_259-1-10.pdf"
        self.assertEqual(books.file_title(url), "File:ДААРК_259-1-10.pdf")


if __name__ == "__main__":
    unittest.main()
