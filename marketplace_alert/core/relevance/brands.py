"""Extensible brand vocabulary used for required-brand / conflicting-brand
detection (see this package's `__init__.py` and PROJECT_CONTEXT.md's
"Relevance filtering" entry).

Deliberately NOT hard-coded to a fixed set of four brands as permanent
architecture - `register_brand()` is the maintainable extension point.
The default catalog below covers common power-tool brands (the domain
that triggered this feature) plus common musical-instrument brands (added
once Reverb/Bonanza made music-gear searches - e.g. "Fender Stratocaster",
"Gibson Les Paul" - a real, live use case; see PROJECT_CONTEXT.md's
product-hardening-pass entry for the audit that found Fender/Gibson/etc.
were missing, which meant a "Fender Stratocaster" search could only ever
reject an actual Gibson listing by accident - via token non-overlap -
rather than the deliberate, robust brand-conflict mechanism every other
domain gets) - but this is all still just a starting seed, registered via
the same public function anyone extending this would use.
"""

from marketplace_alert.core.relevance.text import tokenize

_BRAND_VOCABULARY: dict[tuple[str, ...], str] = {}


def register_brand(canonical: str, aliases: list[str] | None = None) -> None:
    """Register a brand under its canonical name plus any aliases (e.g.
    spacing/punctuation variants). All aliases resolve to the same
    canonical name in `find_phrase_matches()` results, so conflicting-brand
    checks compare canonical names, not surface text.
    """
    all_aliases = [canonical, *(aliases or [])]
    for alias in all_aliases:
        tokens = tuple(tokenize(alias))
        if tokens:
            _BRAND_VOCABULARY[tokens] = canonical


def known_brands() -> frozenset[str]:
    return frozenset(_BRAND_VOCABULARY.values())


def brand_vocabulary() -> dict[tuple[str, ...], str]:
    return dict(_BRAND_VOCABULARY)


def reset_to_defaults() -> None:
    """Clears any registrations (including test-only ones) and restores
    the default catalog. Tests that register a temporary brand should call
    this in teardown so state doesn't leak between tests."""
    _BRAND_VOCABULARY.clear()
    _register_default_brands()


def _register_default_brands() -> None:
    register_brand("makita")
    register_brand("bosch")
    register_brand("dewalt", aliases=["de walt"])
    register_brand("milwaukee")
    register_brand("ryobi")
    register_brand("black+decker", aliases=["black decker", "black and decker"])
    register_brand("craftsman")
    register_brand("hilti")
    register_brand("metabo")
    register_brand("festool")
    register_brand("hitachi")
    register_brand("ridgid")
    register_brand("porter cable", aliases=["porter-cable"])
    register_brand("skil")
    register_brand("worx")
    register_brand("ego")
    register_brand("kobalt")
    # Musical instrument brands (Reverb/Bonanza) - see module docstring.
    register_brand("fender")
    register_brand("squier")
    register_brand("gibson")
    register_brand("epiphone")
    register_brand("ibanez")
    register_brand("prs", aliases=["paul reed smith"])
    register_brand("martin")
    register_brand("taylor")
    register_brand("yamaha")
    register_brand("esp")
    register_brand("jackson")
    register_brand("charvel")
    register_brand("gretsch")
    register_brand("rickenbacker")
    register_brand("schecter")


_register_default_brands()
