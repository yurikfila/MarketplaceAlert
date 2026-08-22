"""Configurable product-family synonym mapping.

A family groups a core product term with the phrases that count as a
*strong* match (genuinely the same product - "hammer drill" is a drill)
versus a *related* match (adjacent, arguably-relevant product - "impact
driver" is not a drill, but someone searching "drill" might still care).
This is what keeps multi-word core-term relevance from degrading to
"one token matched" - see this package's `__init__.py` for the problem
this whole module exists to solve.
"""

from dataclasses import dataclass

from marketplace_alert.core.relevance.text import tokenize


@dataclass(frozen=True)
class ProductFamily:
    core_term: str
    strong_synonyms: tuple[str, ...]
    related_synonyms: tuple[str, ...] = ()


_FAMILIES: dict[str, ProductFamily] = {}


def register_family(core_term: str, strong_synonyms: list[str], related_synonyms: list[str] | None = None) -> None:
    normalized_core = " ".join(tokenize(core_term))
    family = ProductFamily(
        core_term=normalized_core,
        strong_synonyms=tuple(dict.fromkeys([core_term, *strong_synonyms])),
        related_synonyms=tuple(related_synonyms or ()),
    )
    _FAMILIES[normalized_core] = family


def find_family(core_phrase_tokens: list[str]) -> ProductFamily | None:
    """Looks up a family by the query's full core phrase first (e.g.
    "hammer drill"), then falls back to any single token in it (e.g.
    "drill" inside "cordless drill"), so a query naming a synonym phrase
    still finds the right family rather than only exact core-term matches.
    """
    core_phrase = " ".join(core_phrase_tokens)
    if core_phrase in _FAMILIES:
        return _FAMILIES[core_phrase]
    for token in core_phrase_tokens:
        if token in _FAMILIES:
            return _FAMILIES[token]
    return None


def reset_to_defaults() -> None:
    _FAMILIES.clear()
    _register_default_families()


def _register_default_families() -> None:
    register_family(
        "drill",
        strong_synonyms=[
            "drill",
            "hammer drill",
            "rotary drill",
            "cordless drill",
            "driver drill",
            "drill driver",
            "hammer drill driver",
        ],
        related_synonyms=["impact driver", "driver"],
    )


_register_default_families()
