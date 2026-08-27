"""Configurable accessory-term vocabulary.

These are terms that describe an accessory *for* a product rather than
the product itself - a "battery holder" is not a drill, even though a
listing for one might mention "Makita" and share a token with a "Makita
drill" search. Recognizing these lets the evaluator penalize accessory
listings when the user was searching for the main product - but the
evaluator checks the *query* against this same vocabulary too, so a
search that explicitly asks for an accessory (e.g. "Makita battery
holder") is never penalized for matching what it asked for. Since a
product-hardening audit (see PROJECT_CONTEXT.md), a *third* exemption
exists in `evaluator.py`: an "unambiguous" (multi-word) core-product
match also suppresses the penalty - e.g. "Cordless Drill Driver Kit with
Carrying Case and 2 Batteries" is a real drill kit that happens to
mention included accessories, not an accessory-only listing, and must not
be rejected just because "case"/"battery" appear in the same title as a
genuine, unambiguous drill match.

Guitar/music-gear part terms (pickguard, pickup, neck, body, strap,
stand, manual) were added in that same audit for Reverb/Bonanza's
music-gear searches (e.g. "Fender Stratocaster") - these still don't
benefit from the multi-word-match exemption the way "drill" does, since
there's no registered "guitar" product family (a genuine, deliberately
accepted trade-off - see PROJECT_CONTEXT.md decision #18). What *is*
fixed (decision #24): the accessory penalty in `evaluator.py` only
scans the listing **title** for these terms, not the description - a
real production false rejection found three genuine complete Fender
Stratocasters on Reverb scored as accessory-only purely because their
*descriptions* mentioned body/neck/case wood, not because the guitar
itself was an accessory. A title that itself says "Pickguard"/"Body"/
"Case" (an actual part listing) is still correctly rejected either way.
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
        # Power-tool accessories - safe to add now that a multi-word
        # "unambiguous" core match (e.g. "cordless drill", "drill driver")
        # exempts a genuine tool-kit listing from this penalty even when it
        # also mentions an included battery/charger - see module docstring.
        "battery",
        "charger",
        "bag",
        # Musical-instrument/guitar parts (Reverb/Bonanza) - see module
        # docstring for the accepted trade-off with these specifically.
        "pickguard",
        "pickup",
        "neck",
        "body",
        "strap",
        "stand",
        "manual",
    ):
        register_accessory_term(term)


_register_default_accessory_terms()
