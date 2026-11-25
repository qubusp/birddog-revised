"""Latin spellings of a Cyrillic place name, to search core.location, whose
names are Latin: Ukrainian in the BGN/PCGN style ("Kam'yanets'-Podil's'kyy",
"Zhytomyr") and Russian or older spellings among the alternates ("Kanev",
"Zhitomir"). candidates() returns a few spellings, best first:

  ua_bgn   Ukrainian, BGN/PCGN 1965: г h, и y, і i, ї yi, й y, є ye, ю yu, я ya,
           ь ' (the gazetteer's own style)
  ua_kmu   Ukrainian national (2010): є ie, ї i, й i, ю iu, я ia, ь dropped;
           ye/yi/y/yu/ya at the start of a word
  ru       Russian style: г g, и i, ы y, й y, е e (ye after a vowel), ё e,
           ю yu, я ya, ь and ъ dropped
  ru_ua    Russian letters read Ukrainian-style: г h (Belaya -> Bila is
           beyond this; that is what the trigram match is for)

Pre-1918 letters are folded first: ѣ e, і i, ѵ i, ѳ f, ъ at a word end dropped.
Latin input is returned as is."""

import re

_OLD = str.maketrans({"ѣ": "е", "Ѣ": "Е", "ѵ": "и", "Ѵ": "И", "ѳ": "ф", "Ѳ": "Ф"})
_VOWELS = set("аеёиіїоуыэюяєaeiouy")

_COMMON = {"а": "a", "б": "b", "в": "v", "д": "d", "ж": "zh", "з": "z", "к": "k", "л": "l",
           "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
           "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch", "ґ": "g"}

SCHEMES = {
    "ua_bgn": {**_COMMON, "г": "h", "е": "e", "є": "ye", "и": "y", "і": "i", "ї": "yi", "й": "y",
               "ю": "yu", "я": "ya", "ь": "'", "'": "", "’": "", "ʼ": "", "ъ": "", "ы": "y",
               "э": "e", "ё": "e"},
    "ua_kmu": {**_COMMON, "г": "h", "е": "e", "є": "ie", "и": "y", "і": "i", "ї": "i", "й": "i",
               "ю": "iu", "я": "ia", "ь": "", "'": "", "’": "", "ʼ": "", "ъ": "", "ы": "y",
               "э": "e", "ё": "e"},
    "ru": {**_COMMON, "г": "g", "е": "e", "ё": "e", "и": "i", "й": "y", "ы": "y", "э": "e",
           "ю": "yu", "я": "ya", "ь": "", "ъ": "", "і": "i", "ї": "yi", "є": "ye", "'": "",
           "’": "", "ʼ": ""},
}
SCHEMES["ru_ua"] = {**SCHEMES["ru"], "г": "h"}

# Ukrainian national: these letters are written differently at the start of a word
_KMU_INITIAL = {"є": "ye", "ї": "yi", "й": "y", "ю": "yu", "я": "ya"}


def has_cyrillic(text):
    return bool(re.search(r"[Ѐ-ӿ]", text or ""))


def _fold_old(text):
    text = text.translate(_OLD)
    # і before a vowel (pre-1918 Russian) reads as и; ъ at a word end is silent
    text = re.sub(r"ъ\b", "", text)
    return text


def transliterate(text, scheme):
    table = SCHEMES[scheme]
    out, prev = [], ""
    for ch in text:
        low = ch.lower()
        if low in table:
            if scheme == "ua_kmu" and low in _KMU_INITIAL and not prev.isalpha():
                piece = _KMU_INITIAL[low]
            elif scheme in ("ru", "ru_ua") and low == "е" and (not prev.isalpha() or prev.lower() in _VOWELS
                                                              or prev in "ьъ"):
                piece = "ye"
            else:
                piece = table[low]
            if ch.isupper() and piece:
                piece = piece[0].upper() + piece[1:]
            out.append(piece)
        else:
            out.append(ch)
        prev = ch
    return "".join(out)


def candidates(text):
    """Latin spellings to search for, best first, without duplicates."""
    text = (text or "").strip()
    if not has_cyrillic(text):
        return [text] if text else []
    folded = _fold_old(text)
    seen, out = set(), []
    for scheme in ("ua_bgn", "ua_kmu", "ru", "ru_ua"):
        for variant in (transliterate(folded, scheme),):
            for v in (variant, variant.replace("'", "")):
                v = re.sub(r"\s+", " ", v).strip()
                if v and v.lower() not in seen:
                    seen.add(v.lower())
                    out.append(v)
    return out
