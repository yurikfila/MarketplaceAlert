"""Unicode-aware text normalization for the general relevance engine (v2).

Deliberately separate from `text.py`, which the legacy engine, the brand /
accessory vocabularies and their tests depend on and which must keep
behaving exactly as it does today. Everything here is deterministic and
dependency-free: no NLP library, no translation, no external service.

What it does, and what it deliberately does not do:

- Unicode NFKC, case folding, punctuation -> whitespace (apostrophes are
  removed instead, so ``Levi's`` is ``levis``). Latin diacritics
  and Hebrew niqqud/cantillation are dropped; Cyrillic is kept intact
  (``ё`` -> ``е`` is the one exception, the usual Russian convention).
  Non-Latin text is never discarded.
- Hebrew final letter forms are folded to their regular forms.
- Conservative, *symmetric* stemming: the same function is applied to the
  query and to the listing, so a stem only has to be consistent, not
  linguistically perfect. English: plurals and ``-ing``. Russian: common
  case/number endings. Hebrew: a few single-letter prefixes (the/and/in/
  to/from/that/like) and the plural suffixes. Words with digits are never
  stemmed.
- Model numbers / identifiers (``GSB 18V-55``, ``SM-G991B``) are matched on
  a whitespace-insensitive, punctuation-insensitive compact form, so
  ``GSB18V-55`` and ``GSB 18V 55`` are the same identifier.

This is lexical matching. It does not understand meaning across languages:
a Hebrew query will not match an English-only title unless a brand alias
or the product word itself appears in both.
"""

import re
import unicodedata

_HEBREW_FINAL_FORMS = str.maketrans({"ך": "כ", "ם": "מ", "ן": "נ", "ף": "פ", "ץ": "צ"})
_NON_WORD = re.compile(r"[\W_]+", re.UNICODE)
_QUANTITY = re.compile(r"^(?:\d+x?|x\d+)$")

# Single-letter Hebrew prefixes (the / and / in / to / from / that / like).
_HEBREW_PREFIXES = frozenset("הובלמשכ")
# Longer suffixes first.
_HEBREW_SUFFIXES = ("ים", "ות")
_RUSSIAN_ENDINGS = (
    "ого", "ему", "ыми", "ими", "ами", "ями", "ией", "ьей", "ью", "ое", "ая", "ую", "ые", "ых", "их", "ой", "ей",
    "ом", "ем", "ам", "ям", "ов", "ев", "ах", "ях", "ы", "и", "а", "я", "у", "ю", "е", "о",
)  # fmt: skip


def normalize_unicode(text: str | None) -> str:
    """Lower-cased, punctuation-free, single-spaced text (may be empty)."""
    if not text:
        return ""
    out: list[str] = []
    for char in unicodedata.normalize("NFKC", text):
        if "֑" <= char <= "ׇ":  # Hebrew cantillation + niqqud
            continue
        if char in "'’ʼ׳":  # apostrophes join, not split: Levi's -> levis, women's -> womens
            continue
        if char in "ёЁ":
            out.append("е")
            continue
        if char in "йЙ":
            out.append("й")
            continue
        decomposed = unicodedata.normalize("NFD", char)
        if len(decomposed) > 1:
            char = "".join(c for c in decomposed if not unicodedata.combining(c))
        out.append(char)
    folded = "".join(out).casefold().translate(_HEBREW_FINAL_FORMS)
    return _NON_WORD.sub(" ", folded).strip()


def _script(token: str) -> str:
    for char in token:
        if "֐" <= char <= "׿":
            return "hebrew"
        if "Ѐ" <= char <= "ӿ":
            return "cyrillic"
        if char.isalpha():
            return "latin"
    return "other"


def _stem_latin(token: str) -> str:
    stem = token
    if len(stem) > 4 and stem.endswith("ies"):
        stem = stem[:-3] + "y"
    elif len(stem) > 4 and stem.endswith(("ches", "shes", "xes", "zes", "sses")):
        stem = stem[:-2]
    elif len(stem) > 3 and stem.endswith("s") and not stem.endswith(("ss", "us", "is")):
        stem = stem[:-1]
    if len(stem) > 6 and stem.endswith("ing"):
        stem = stem[:-3]
        if len(stem) > 3 and stem[-1] == stem[-2] and stem[-1] not in "lsz":
            stem = stem[:-1]  # running -> run (but drilling -> drill, pressing -> press)
    return stem


def _stem_cyrillic(token: str) -> str:
    stem = token
    for ending in _RUSSIAN_ENDINGS:
        if stem.endswith(ending) and len(stem) - len(ending) >= 3:
            stem = stem[: -len(ending)]
            break
    if len(stem) > 3 and stem[-1] in "ьй":
        stem = stem[:-1]
    return stem


def _stem_hebrew(token: str) -> str:
    stem = token
    for _ in range(2):  # "ה" + "מקדחה" -> up to two stacked prefixes
        if len(stem) >= 5 and stem[0] in _HEBREW_PREFIXES:
            stem = stem[1:]
    for suffix in _HEBREW_SUFFIXES:
        if stem.endswith(suffix) and len(stem) - len(suffix) >= 3:
            stem = stem[: -len(suffix)]
            break
    if len(stem) >= 4 and stem.endswith("ה"):
        stem = stem[:-1]  # singular -ה / plural -ות share one stem (מקדחה / מקדחות)
    return stem


def stem_token(token: str) -> str:
    if any(char.isdigit() for char in token):
        return token
    script = _script(token)
    if script == "latin":
        return _stem_latin(token)
    if script == "cyrillic":
        return _stem_cyrillic(token)
    if script == "hebrew":
        return _stem_hebrew(token)
    return token


def tokenize_raw(text: str | None) -> list[str]:
    """Normalized, un-stemmed tokens."""
    normalized = normalize_unicode(text)
    return normalized.split(" ") if normalized else []


def tokenize_stems(text: str | None) -> list[str]:
    return [stem_token(token) for token in tokenize_raw(text)]


def is_quantity(token: str) -> bool:
    """``2``, ``2x``, ``x2`` - a count of included items, not a model number."""
    return bool(_QUANTITY.match(token))


def has_digit(token: str) -> bool:
    return any(char.isdigit() for char in token)


def is_strong_identifier(token_group: str) -> bool:
    """Looks like a model number: at least two letters and two digits once
    compacted (``gsb18v55``, ``g991b``, ``iphone13``). ``18v``/``5ah``/``500w``
    are specs, not model numbers, and are matched as ordinary terms."""
    letters = sum(1 for c in token_group if c.isalpha())
    digits = sum(1 for c in token_group if c.isdigit())
    return letters >= 2 and digits >= 2


def compact_windows(raw_tokens: list[str], max_len: int = 4) -> set[str]:
    """Every concatenation of 1..`max_len` consecutive raw tokens - the
    spacing/punctuation-insensitive forms a model number might take."""
    windows: set[str] = set()
    for start in range(len(raw_tokens)):
        joined = ""
        for end in range(start, min(start + max_len, len(raw_tokens))):
            joined += raw_tokens[end]
            windows.add(joined)
    return windows
