"""Pure-logic tests for birddog/books.py (no network, no database)."""

import unittest
import unittest.mock

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

    def test_estimate_matches_pilot(self):
        # pilot, narrow rule: 380 pages at ~4175 probe tokens cost $4.96 all in,
        # about $0.013 a page; a 500k-token book is about 104 pages
        tokens, usd = books.estimate(100, 4175)
        self.assertAlmostEqual(usd / 100, 0.0128, delta=0.001)
        self.assertLess(books.estimate(104, 4175)[0], 500_000)
        self.assertGreater(books.estimate(106, 4175)[0], 500_000)

    def test_estimate_includes_rereads(self):
        with unittest.mock.patch.object(books, "REREAD_SHARE", 0):
            _, sonnet_only = books.estimate(100, 4175)
        _, with_rereads = books.estimate(100, 4175)
        self.assertGreater(with_rereads, sonnet_only)

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
        self.assertTrue(books._hard_page(self.PAGES[4]))      # illegible
        self.assertFalse(books._hard_page(self.PAGES[5]))     # one low-confidence place only
        self.assertFalse(books._hard_page(self.PAGES[3]))

    def test_hard_page_pilot_rule(self):
        with unittest.mock.patch.object(books, "REREAD_LOW_CONFIDENCE", True):
            self.assertTrue(books._hard_page(self.PAGES[4]))
            self.assertTrue(books._hard_page(self.PAGES[5]))
            self.assertFalse(books._hard_page(self.PAGES[3]))


class FileTitleTests(unittest.TestCase):
    def test_file_title(self):
        url = "https://commons.wikimedia.org/wiki/File:%D0%94%D0%90%D0%90%D0%A0%D0%9A_259-1-10.pdf"
        self.assertEqual(books.file_title(url), "File:ДААРК_259-1-10.pdf")

    def test_file_title_without_wiki_prefix(self):
        url = "https://commons.wikimedia.org/File:Фонд_1165_Опись_1_Дело_5.pdf"
        self.assertEqual(books.file_title(url), "File:Фонд_1165_Опись_1_Дело_5.pdf")

    def test_not_a_file(self):
        with self.assertRaises(books.BookError):
            books.file_title("https://commons.wikimedia.org/wiki/Category:Foo")


if __name__ == "__main__":
    unittest.main()
