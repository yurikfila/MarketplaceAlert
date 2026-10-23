"""General-purpose relevance engine (v2): `evaluate_relevance_v2(query, listing)`.

A lexical, deterministic, explainable scorer that needs no per-product
registration. The legacy engine (`evaluator.py`) is untouched and remains
the default; `service.py` selects between them with the `RELEVANCE_ENGINE`
setting (`legacy` | `v2` | `shadow`).

How a query is interpreted (`parse_query_v2`)
---------------------------------------------
- Words are tokenized with Unicode normalization and conservative,
  symmetric stemming (`normalization.py`).
- A *brand* is only something in the brand vocabulary (`brands.py` plus a
  small set of Hebrew/Russian transliterations below). An unknown word is
  never guessed to be a brand - it is an ordinary product term, so a
  previously unseen brand or category is simply matched lexically.
- Everything else is a *product term* (stop words dropped). A whitespace-
  separated chunk containing digits is an *identifier* term (``18V-55``,
  ``SM-G991B``): matched on its compact form, so spacing/punctuation
  differences do not matter. Identifier terms are always required.
- A query made only of brands is a *broad* (brand-only) search: any listing
  that mentions the brand is accepted. That is intentional and preserved.

How a listing is scored (all components are reported in `breakdown`)
-------------------------------------------------------------------
    product   0..70   weighted share of the query's product terms found in the
                      listing TITLE (word order irrelevant). Words weigh 1,
                      terms containing digits 2, model-number-like terms 3.
    phrase    0..15   bonus when the terms appear contiguously in query
                      order (smaller bonus when merely close together).
    brand     0..15   15 when the listing names the queried brand in the
                      title, 8 when only in the description or when the
                      listing names no brand at all (not penalized), 15 when
                      the query has no brand.
    accessory  -50    the title names an accessory / part word the query did
                      not ask for, directly as the thing for sale (not
                      "with ...", not a counted extra like "2 batteries").
    compat     -50    the query's product terms only appear after a
                      "for / fits / compatible" marker: the listing is
                      *for* the product, not the product.
Hard rules, independent of the score: a query term that is missing is a
rejection (a long query may miss `(n-1)//3` plain words - never a digit
term), a *different* known brand in the title without the queried brand is
a rejection, and a blank title is a rejection. The accept threshold
(`relevance_min_score`, default 55) is configurable.

Limitations (deliberate - this does not understand meaning)
-----------------------------------------------------------
No translation or cross-language semantics, no knowledge of what a word
*means*: "Bosch drill" cannot know that a dishwasher pump is unrelated
except because "drill" is absent from its title. Product words that are
accessories in one context and products in another ("case", "bag") rely
on the query to say which is wanted.
"""

import re
from dataclasses import dataclass

from marketplace_alert.core.models.listing import Listing
from marketplace_alert.core.relevance import accessories, brands, families
from marketplace_alert.core.relevance.models import RelevanceEvaluation
from marketplace_alert.core.relevance.text import tokenize as legacy_tokenize
from marketplace_alert.core.relevance.normalization import (
    compact_windows,
    has_digit,
    is_quantity,
    is_strong_identifier,
    normalize_unicode,
    stem_token,
    tokenize_raw,
)

DEFAULT_MIN_SCORE = 55

PRODUCT_MAX = 70
PHRASE_BONUS_CONTIGUOUS = 15
PHRASE_BONUS_CLOSE = 8
PHRASE_BONUS_SINGLE = 10
BRAND_TITLE = 15
BRAND_WEAK = 8
ACCESSORY_PENALTY = 50
COMPATIBILITY_PENALTY = 50
RELATED_FAMILY_CREDIT = 0.6

_INCLUSION_MARKERS = frozenset({"with", "w", "incl", "includes", "including", "plus", "и", "с", "עם", "כולל", "בצירוף"})
_INCLUSION_WINDOW = 4
_CONJUNCTIONS = frozenset({"and", "и", "ו"})
_COMPATIBILITY_MARKERS = frozenset({"for", "fits", "fit", "compatible", "compatibility", "для", "подходит", "עבור", "מתאים"})
_STOP_WORDS = frozenset(
    {
        "a", "an", "the", "for", "of", "and", "or", "with", "in", "on", "to", "by", "from", "w",
        "и", "в", "на", "для", "с", "по", "от", "из", "к", "о",
        "של", "עם", "את", "או", "על",
    }
)  # fmt: skip

_STOP_STEMS = frozenset(stem_token(normalize_unicode(w)) for w in _STOP_WORDS)

# Seed transliterations so a Hebrew/Russian query or listing can still name
# a known brand. Extending this is data, not code; an unlisted brand simply
# behaves as an ordinary product word.
_BRAND_TRANSLITERATIONS: dict[str, tuple[str, ...]] = {
    "bosch": ("בוש", "бош"),
    "makita": ("מקיטה", "макита"),
    "dewalt": ("דיוולט", "деволт", "девольт"),
    "milwaukee": ("מילווקי", "милуоки"),
    "ryobi": ("ריובי", "риоби"),
    "hilti": ("הילטי", "хилти"),
    "metabo": ("מטבו", "метабо"),
    "festool": ("פסטול", "фестул"),
    "hitachi": ("היטאצי", "хитачи"),
    "fender": ("פנדר", "фендер"),
    "gibson": ("גיבסון", "гибсон"),
    "yamaha": ("ימאהה", "ямаха"),
    "ibanez": ("איבנז", "ибанез"),
    "epiphone": ("אפיפון", "эпифон"),
}

# Words that name an accessory / spare part / consumable. Merged with the
# legacy accessory vocabulary (single-word entries). Used only to decide
# whether a listing is *selling* such a thing the query did not ask for.
_EXTRA_ACCESSORY_WORDS = (
    "bit", "chuck", "spare", "part", "replacement", "cover", "sleeve", "protector", "cable", "bracket",
    "נרתיק", "מטען", "סוללה", "תיק", "חלפים", "חלק", "כיסוי", "מעמד", "מתאם", "רצועה",
    "чехол", "зарядка", "зарядное", "аккумулятор", "батарея", "сумка", "запчасть", "кейс", "держатель",
    "крепление", "адаптер", "ремень",
)  # fmt: skip


@dataclass(frozen=True)
class QueryTerm:
    display: str
    stem: str
    compact: str  # raw alnum form, used for identifier matching
    weight: int
    is_identifier: bool
    must_match: bool


@dataclass(frozen=True)
class ParsedQueryV2:
    raw: str
    terms: tuple[QueryTerm, ...]
    brands: frozenset[str]

    @property
    def is_empty(self) -> bool:
        return not self.terms and not self.brands

    @property
    def is_brand_only(self) -> bool:
        return not self.terms and bool(self.brands)

    @property
    def stems(self) -> frozenset[str]:
        return frozenset(term.stem for term in self.terms)


def _brand_vocabulary() -> dict[tuple[str, ...], str]:
    vocabulary: dict[tuple[str, ...], str] = {}
    for legacy_tokens, canonical in brands.brand_vocabulary().items():
        # Legacy tokens are legacy-singularized; re-derive from the phrase
        # text so v2 stemming applies uniformly.
        key = tuple(stem_token(t) for t in tokenize_raw(" ".join(legacy_tokens)))
        if key:
            vocabulary[key] = canonical
        canonical_key = tuple(stem_token(t) for t in tokenize_raw(canonical))
        if canonical_key:
            vocabulary[canonical_key] = canonical
    for canonical, aliases in _BRAND_TRANSLITERATIONS.items():
        for alias in aliases:
            key = tuple(stem_token(t) for t in tokenize_raw(alias))
            if key:
                vocabulary[key] = canonical
    return vocabulary


def _accessory_stems() -> frozenset[str]:
    words: set[str] = set()
    for legacy_tokens in accessories.accessory_vocabulary():
        if len(legacy_tokens) == 1:  # multi-word entries ("accessory kit") would make "kit" an accessory
            words.add(stem_token(normalize_unicode(legacy_tokens[0])))
    for word in _EXTRA_ACCESSORY_WORDS:
        words.add(stem_token(normalize_unicode(word)))
    return frozenset(words)


def _find_brands(stems: list[str], vocabulary: dict[tuple[str, ...], str]) -> dict[int, tuple[int, str]]:
    """start index -> (length, canonical brand), longest phrase first, non-overlapping."""
    found: dict[int, tuple[int, str]] = {}
    max_len = max((len(k) for k in vocabulary), default=0)
    index = 0
    while index < len(stems):
        matched = False
        for length in range(min(max_len, len(stems) - index), 0, -1):
            canonical = vocabulary.get(tuple(stems[index : index + length]))
            if canonical is not None:
                found[index] = (length, canonical)
                index += length
                matched = True
                break
        if not matched:
            index += 1
    return found


def parse_query_v2(query: str | None) -> ParsedQueryV2:
    vocabulary = _brand_vocabulary()
    normalized_chunks = [chunk for chunk in re.split(r"\s+", query or "") if chunk]
    # Tokenize per whitespace-chunk so "GSB18V-55" / "18V-55" stay one identifier.
    items: list[tuple[str, str, bool]] = []  # (display, stem-or-compact, chunk_has_digit)
    for chunk in normalized_chunks:
        raw_tokens = tokenize_raw(chunk)
        if not raw_tokens:
            continue
        if any(has_digit(t) for t in raw_tokens):
            compact = "".join(raw_tokens)
            items.append((chunk, compact, True))
        else:
            for token in raw_tokens:
                items.append((token, stem_token(token), False))

    word_stems = [stem for _, stem, is_id in items if not is_id]
    brand_spans = _find_brands([s for _, s, _ in items], vocabulary)
    brand_index: set[int] = set()
    found_brands: set[str] = set()
    for start, (length, canonical) in brand_spans.items():
        # Only plain words can be brand names, never identifiers.
        if any(items[i][2] for i in range(start, start + length)):
            continue
        brand_index.update(range(start, start + length))
        found_brands.add(canonical)

    terms: list[QueryTerm] = []
    for i, (display, stem, is_id) in enumerate(items):
        if i in brand_index:
            continue
        if not is_id and stem in _STOP_STEMS and len(word_stems) > 1:
            continue
        strong = is_id and is_strong_identifier(stem)
        terms.append(
            QueryTerm(
                display=display,
                stem=stem,
                compact=stem if is_id else "".join(tokenize_raw(display)),
                weight=3 if strong else (2 if is_id else 1),
                is_identifier=is_id,
                must_match=is_id,
            )
        )
    return ParsedQueryV2(raw=query or "", terms=tuple(terms), brands=frozenset(found_brands))


def _phrase_bonus(parsed: ParsedQueryV2, matched_positions: dict[str, int], title_stems: list[str]) -> int:
    count = len(parsed.terms)
    if count == 0 or len(matched_positions) < count:
        return 0
    if count == 1:
        return PHRASE_BONUS_SINGLE
    ordered = [t.stem for t in parsed.terms]
    for start in range(len(title_stems) - count + 1):
        if title_stems[start : start + count] == ordered:
            return PHRASE_BONUS_CONTIGUOUS
    positions = sorted(matched_positions.values())
    if positions[-1] - positions[0] <= count + 1:
        return PHRASE_BONUS_CLOSE
    return 0


def _unrequested_accessories(
    title_raw: list[str], title_stems: list[str], requested: frozenset[str], accessory_words: frozenset[str]
) -> list[str]:
    """Accessory words the title sells directly (not via "with ...", not a
    counted extra, not part of an included list) that the query did not ask for."""
    marker_positions = [i for i, token in enumerate(title_raw) if token in _INCLUSION_MARKERS]
    exempt: set[int] = set()
    found: list[str] = []
    for index, stem in enumerate(title_stems):
        if stem not in accessory_words or stem in requested:
            continue
        included = any(marker < index <= marker + _INCLUSION_WINDOW for marker in marker_positions)
        counted = index > 0 and is_quantity(title_raw[index - 1])
        listed = index > 1 and title_raw[index - 1] in _CONJUNCTIONS and (index - 2) in exempt
        if included or counted or listed:
            exempt.add(index)
            continue
        found.append(title_raw[index])
    return found


def evaluate_relevance_v2(query: str | None, listing: Listing, *, min_score: int = DEFAULT_MIN_SCORE) -> RelevanceEvaluation:
    parsed = parse_query_v2(query)
    if parsed.is_empty:
        return RelevanceEvaluation(False, 0, [], "empty_query")

    title = listing.title or ""
    title_raw = tokenize_raw(title)
    if not title_raw:
        return RelevanceEvaluation(False, 0, [], "missing_title")
    title_stems = [stem_token(t) for t in title_raw]
    description_stems = [stem_token(t) for t in tokenize_raw(listing.description)]

    vocabulary = _brand_vocabulary()
    title_brands = {canonical for _, canonical in _find_brands(title_stems, vocabulary).values()}
    description_brands = {canonical for _, canonical in _find_brands(description_stems, vocabulary).values()}

    # --- brand handling ---------------------------------------------------
    if parsed.brands:
        if title_brands and not (title_brands & parsed.brands):
            return RelevanceEvaluation(False, 15, [], "brand_conflict", breakdown={"brand": 0})
        if title_brands & parsed.brands:
            brand_points = BRAND_TITLE
        else:
            brand_points = BRAND_WEAK  # description-only mention, or brand-neutral title
    else:
        brand_points = BRAND_TITLE

    if parsed.is_brand_only:
        if (title_brands | description_brands) & parsed.brands:
            points = BRAND_TITLE if title_brands & parsed.brands else BRAND_WEAK
            return RelevanceEvaluation(
                True, 85 if points == BRAND_TITLE else 60, sorted(parsed.brands), None, breakdown={"brand": 100}
            )
        return RelevanceEvaluation(False, 0, [], "brand_only_not_mentioned")

    # --- product term coverage (title only) -------------------------------
    windows = compact_windows(title_raw)
    related_phrases = _related_family_phrases(parsed)
    title_stem_set = set(title_stems)

    matched_weight = 0.0
    total_weight = sum(t.weight for t in parsed.terms)
    matched_positions: dict[str, int] = {}
    matched_terms: list[str] = sorted(parsed.brands & title_brands)
    missing: list[QueryTerm] = []
    for term in parsed.terms:
        if term.is_identifier:
            hit = term.compact in windows
        else:
            hit = term.stem in title_stem_set
        if hit:
            matched_weight += term.weight
            matched_terms.append(term.display)
            if not term.is_identifier:
                matched_positions[term.stem] = title_stems.index(term.stem)
            continue
        if not term.is_identifier and any(_contains_phrase(title_stems, p) for p in related_phrases.get(term.stem, ())):
            matched_weight += term.weight * RELATED_FAMILY_CREDIT
            matched_terms.append(term.display)
            continue
        missing.append(term)

    still_missing = _rescue_split_forms(parsed, missing, windows, set(title_raw), matched_terms)
    matched_weight += sum(t.weight for t in missing if t not in still_missing)
    missing = still_missing
    plain_missing = [t for t in missing if not t.must_match]
    required_missing = [t for t in missing if t.must_match]
    allowed_missing = max(0, (len([t for t in parsed.terms if not t.must_match]) - 1) // 3)
    breakdown = {
        "product": round(PRODUCT_MAX * matched_weight / total_weight),
        "phrase": 0,
        "brand": brand_points,
        "accessory": 0,
        "compatibility": 0,
    }
    if required_missing:
        reason = "missing_model_identifier" if any(is_strong_identifier(t.compact) for t in required_missing) else "missing_required_terms"
        return RelevanceEvaluation(
            False, max(0, sum(breakdown.values())), matched_terms, reason, breakdown=breakdown,
            missing_terms=tuple(t.display for t in missing),
        )  # fmt: skip
    if len(plain_missing) > allowed_missing:
        return RelevanceEvaluation(
            False, max(0, sum(breakdown.values())), matched_terms, "missing_required_terms", breakdown=breakdown,
            missing_terms=tuple(t.display for t in missing),
        )  # fmt: skip

    breakdown["phrase"] = _phrase_bonus(parsed, matched_positions, title_stems)

    # --- contradictions ----------------------------------------------------
    accessory_hits = _unrequested_accessories(title_raw, title_stems, parsed.stems, _accessory_stems())
    if accessory_hits:
        breakdown["accessory"] = -ACCESSORY_PENALTY
    marker_index = next(
        (
            i
            for i, token in enumerate(title_raw)
            if token in _COMPATIBILITY_MARKERS and not (token == "for" and title_raw[i + 1 : i + 2] == ["sale"])
        ),
        None,
    )
    if marker_index is not None and matched_positions:
        if all(position > marker_index for position in matched_positions.values()):
            breakdown["compatibility"] = -COMPATIBILITY_PENALTY

    score = max(0, min(100, sum(breakdown.values())))
    if score < min_score:
        if breakdown["accessory"]:
            reason = "accessory_mismatch"
        elif breakdown["compatibility"]:
            reason = "compatibility_target_only"
        else:
            reason = "low_relevance_score"
        return RelevanceEvaluation(False, score, matched_terms, reason, breakdown=breakdown)
    return RelevanceEvaluation(True, score, matched_terms, None, breakdown=breakdown)


def _rescue_split_forms(
    parsed: ParsedQueryV2,
    missing: list[QueryTerm],
    windows: set[str],
    title_tokens: set[str],
    matched_terms: list[str],
) -> list[QueryTerm]:
    """Two spelling variants that term-by-term matching cannot see:

    - A model number written "GSB 18V-55" in the query but "GSB18V-55" in the
      title (or "iPhone 13" / "iPhone13"): the *run* of adjacent query terms
      joined together is present.
    - An initialism: "Game Boy Advance" in the query, "GBA" in the title -
      3+ adjacent plain words whose initials form a title token.

    Terms rescued this way count as matched."""
    if not missing:
        return missing
    terms = list(parsed.terms)
    rescued: set[int] = set()
    for start in range(len(terms)):
        for end in range(start + 2, min(start + 4, len(terms)) + 1):
            run = terms[start:end]
            if not any(t.is_identifier for t in run):
                continue
            joined = "".join(t.compact for t in run)
            if joined in windows:
                rescued.update(range(start, end))
    for start in range(len(terms)):
        for end in range(start + 3, min(start + 5, len(terms)) + 1):
            run = terms[start:end]
            if any(t.is_identifier for t in run):
                continue
            initials = "".join(t.compact[:1] for t in run)
            if initials in title_tokens:
                rescued.update(range(start, end))
    still_missing = []
    for index, term in enumerate(terms):
        if term in missing:
            if index in rescued:
                matched_terms.append(term.display)
            else:
                still_missing.append(term)
    return still_missing


def _related_family_phrases(parsed: ParsedQueryV2) -> dict[str, list[tuple[str, ...]]]:
    """Registered product families (optional enrichment, e.g. "drill") may
    grant partial credit when a *related* phrase ("impact driver") appears
    in full. Nothing requires a family to exist for a query to work."""
    related: dict[str, list[tuple[str, ...]]] = {}
    for term in parsed.terms:
        if term.is_identifier:
            continue
        family = families.find_family(legacy_tokenize(term.display))
        if family is None:
            continue
        phrases = [tuple(stem_token(t) for t in tokenize_raw(phrase)) for phrase in family.related_synonyms]
        phrases = [p for p in phrases if p and p != (term.stem,)]
        if phrases:
            related[term.stem] = phrases
    return related


def _contains_phrase(stems: list[str], phrase: tuple[str, ...]) -> bool:
    size = len(phrase)
    return any(tuple(stems[i : i + size]) == phrase for i in range(len(stems) - size + 1))
