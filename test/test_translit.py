"""birddog/translit.py: Latin spellings of Cyrillic place names, as core.location writes them."""

import pytest

from birddog.translit import candidates, has_cyrillic


@pytest.mark.parametrize("cyrillic, expected", [
    ("Канів", "Kaniv"),
    ("Канев", "Kanev"),
    ("Біла Церква", "Bila Tserkva"),
    ("Житомир", "Zhytomyr"),
    ("Житомир", "Zhitomir"),
    ("Лысянка", "Lysyanka"),
    ("Лисянка", "Lysyanka"),
    ("Шпиков", "Shpikov"),
    ("Шпиків", "Shpykiv"),
    ("Ольшанка", "Ol'shanka"),
    ("Ольшанка", "Olshanka"),
    ("Калигорка", "Kalyhorka"),
    ("Калигорка", "Kaligorka"),
    ("Звенигородка", "Zvenyhorodka"),
    ("Звенигородка", "Zvenigorodka"),
    ("Острогъ", "Ostrog"),          # pre-1918 hard sign at the end
    ("Острог", "Ostroh"),
    ("Бѣлая Церковь", "Belaya Tserkov"),
    ("Ѳеодосія", "Feodosiya"),
    ("Ямпіль", "Yampil'"),
    ("Ямпіль", "Yampil"),           # Ukrainian national: ya at the start of a word
    ("Лисянка", "Lysianka"),        # ... and ia inside it
])
def test_candidates_include(cyrillic, expected):
    assert expected in candidates(cyrillic)


def test_order_and_no_duplicates():
    c = candidates("Житомир")
    assert c[0] == "Zhytomyr"                 # the gazetteer's own (Ukrainian BGN) style first
    assert len(c) == len({x.lower() for x in c})


def test_latin_passes_through():
    assert candidates("  Kaniv ") == ["Kaniv"]
    assert candidates("") == []
    assert has_cyrillic("Kaniv") is False and has_cyrillic("Канів") is True


def test_case_is_kept():
    assert candidates("ЖИТОМИР")[0].startswith("Zh")
