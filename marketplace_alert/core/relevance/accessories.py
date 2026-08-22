"""Configurable accessory-term vocabulary.

These are terms that describe an accessory *for* a product rather than
the product itself - a "battery holder" is not a drill, even though a
listing for one might mention "Makita" and share a token with a "Makita
drill" search. Recognizing these lets the evaluator penalize accessory
listings when the user was searching for the main product - but the
evaluator checks the *query* against this same vocabulary too, so a
search that explicitly asks for an accessory (e.g. "Makita battery
holder") is never penalized for matching what it asked for.
"""

from marketplace_alert.core.relevance.text import tokenize

_ACCESSORY_VOCABULARY: dict[tuple[str, ...], str] = {}


def register_accessory_term(phrase: str) -> None:
    tokens = tuple(tokenize(phrase))
    if tokens:
        _ACCESSORY_VOCABULARY[tokens] = " ".join(tokens)


def accessory_vocabulary() -> dict[tuple[str, ...], str]:
    return dict(_ACCESSORY_VOCABULARY)


def reset_to_defaults() -> None:
    _ACCESSORY_VOCABULARY.clear()
    _register_default_accessory_terms()


def _register_default_accessory_terms() -> None:
    for term in (
        "holder",
        "mount",
        "organizer",
        "case",
        "clip",
        "rack",
        "adapter",
        "bit holder",
        "battery holder",
        "wall mount",
    ):
        register_accessory_term(term)


_register_default_accessory_terms()
