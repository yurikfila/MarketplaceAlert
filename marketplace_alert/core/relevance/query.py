"""Parses a saved search's raw query string into tokens, any recognized
brand(s), and the remaining "core" tokens (the query with brand tokens
removed) - the shape the evaluator scores against.
"""

from dataclasses import dataclass

from marketplace_alert.core.relevance import brands
from marketplace_alert.core.relevance.text import find_phrase_matches, tokenize


@dataclass(frozen=True)
class ParsedQuery:
    raw: str
    tokens: list[str]
    brand_tokens: tuple[str, ...]
    brands: frozenset[str]
    core_tokens: list[str]


def parse_query(query: str) -> ParsedQuery:
    tokens = tokenize(query)
    brand_matches = find_phrase_matches(tokens, brands.brand_vocabulary())
    matched_brand_token_tuples = [tuple(phrase.split(" ")) for phrase in brand_matches]
    canonical_brands = frozenset(brand_matches.values())

    removed_indices: set[int] = set()
    for phrase_tuple in matched_brand_token_tuples:
        n = len(phrase_tuple)
        for start in range(len(tokens) - n + 1):
            if tuple(tokens[start : start + n]) == phrase_tuple:
                removed_indices.update(range(start, start + n))
    core_tokens = [t for i, t in enumerate(tokens) if i not in removed_indices]
    brand_tokens = tuple(t for i, t in enumerate(tokens) if i in removed_indices)

    return ParsedQuery(
        raw=query,
        tokens=tokens,
        brand_tokens=brand_tokens,
        brands=canonical_brands,
        core_tokens=core_tokens,
    )
