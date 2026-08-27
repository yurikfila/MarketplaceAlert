"""Text normalization and generic phrase (n-gram) matching primitives for
the relevance module. Deliberately dependency-free - no NLP library,
no embeddings (see PROJECT_CONTEXT.md/ARCHITECTURE.md "Relevance
filtering" for why a lightweight, fully deterministic approach was
chosen). Every other module in `core/relevance/` builds on these two
functions.
"""

import re
from collections.abc import Mapping

_PUNCTUATION_PATTERN = re.compile(r"[^\w\s]")
_WHITESPACE_PATTERN = re.compile(r"\s+")


def _singularize(token: str) -> str:
    """A conservative trailing-s heuristic, not a real stemmer/lemmatizer.

    Skips short tokens and ones already ending "ss" (e.g. "class") to
    avoid mangling them. Good enough for this domain's realistic
    vocabulary (batteries -> battery, holders -> holder, drills -> drill);
    not a substitute for a real NLP library, and not meant to be - see
    this package's docstring for why that trade-off was made deliberately.
    """
    if len(token) > 3 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def normalize_text(text: str) -> str:
    """lowercase, strip punctuation (including common separators like
    `-`/`/`/`,`/`&`, all matched by "not a word character"), collapse
    whitespace to single spaces, and trim."""
    lowered = text.lower()
    no_punctuation = _PUNCTUATION_PATTERN.sub(" ", lowered)
    collapsed = _WHITESPACE_PATTERN.sub(" ", no_punctuation).strip()
    return collapsed


def tokenize(text: str) -> list[str]:
    """`normalize_text()` + split on whitespace + simple plural tolerance.
    Empty list for blank/whitespace-only input."""
    normalized = normalize_text(text)
    if not normalized:
        return []
    return [_singularize(token) for token in normalized.split(" ")]


def find_phrase_matches(tokens: list[str], vocabulary: Mapping[tuple[str, ...], str]) -> dict[str, str]:
    """Every vocabulary entry present as a contiguous token n-gram in
    `tokens` - i.e. matched on whole words, never a substring inside a
    longer unrelated word (so "drill" never accidentally matches inside
    "drilled", for example - they tokenize to different tokens entirely).

    Returns `{matched phrase (space-joined): vocabulary's mapped value}`.
    The same primitive backs brand detection (`brands.py`), accessory-term
    detection (`accessories.py`), and product-family synonym matching
    (`families.py`, via the evaluator) - one "is any of these known
    phrases present" implementation, not three.
    """
    if not tokens or not vocabulary:
        return {}
    matches: dict[str, str] = {}
    max_n = max(len(phrase) for phrase in vocabulary)
    for n in range(1, min(max_n, len(tokens)) + 1):
        for start in range(len(tokens) - n + 1):
            window = tuple(tokens[start : start + n])
            mapped = vocabulary.get(window)
            if mapped is not None:
                matches[" ".join(window)] = mapped
    return matches


def find_phrase_match_positions(
    tokens: list[str], vocabulary: Mapping[tuple[str, ...], str]
) -> list[tuple[int, int, str]]:
    """Like `find_phrase_matches()`, but returns `(start_index,
    end_index_exclusive, canonical_value)` for every match instead of a
    phrase-keyed dict - for a caller that needs to reason about *where* a
    match occurred (e.g. whether an accessory term is immediately preceded
    by an inclusion marker like "with"/"w/", meaning it describes a
    feature of the product being sold rather than being the product for
    sale itself - see `evaluator.py`'s accessory-penalty exemption).
    Every other caller that only needs "is this phrase present" should
    keep using `find_phrase_matches()` - this is deliberately a separate,
    additive function rather than a signature change, so nothing else has
    to reason about positions it doesn't need.
    """
    if not tokens or not vocabulary:
        return []
    matches: list[tuple[int, int, str]] = []
    max_n = max(len(phrase) for phrase in vocabulary)
    for n in range(1, min(max_n, len(tokens)) + 1):
        for start in range(len(tokens) - n + 1):
            window = tuple(tokens[start : start + n])
            mapped = vocabulary.get(window)
            if mapped is not None:
                matches.append((start, start + n, mapped))
    return matches
