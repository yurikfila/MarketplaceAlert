"""The relevance scoring engine: `evaluate_relevance(query, listing)`.

Scoring, in order:

1. **Brand conflict** - if the query names a brand and the listing names a
   *different* recognized brand, reject immediately (`brand_conflict`).
   This is checked before anything else because a conflicting brand makes
   a listing wrong regardless of how well other terms match.
2. **Brand match bonus** - if the listing's brand matches the query's,
   add `BRAND_MATCH_BONUS`. A brand-neutral listing (no recognized brand
   at all) is not penalized - plenty of real listings just don't mention
   a brand in the title.
3. **Core-term match** - the query's remaining (non-brand) tokens are
   scored against a registered product family (`families.py`) if one
   applies: a *strong* synonym (e.g. "hammer drill" for the "drill"
   family) scores `STRONG_CORE_MATCH_SCORE`, a *related* one (e.g.
   "impact driver") scores the lower `RELATED_CORE_MATCH_SCORE`. If the
   query itself is an accessory phrase (e.g. "battery holder" with no
   brand), the listing must contain that same accessory phrase. If
   neither a family nor an accessory phrase applies (the query names some
   product this vocabulary doesn't know about), fall back to a lenient
   "any core token present" check - see `_score_core_match` for why this
   fallback is deliberately not proportional/stricter.
4. **Accessory penalty** - if the listing itself contains an accessory
   term (holder, mount, case, ...) and the *query* did not ask for one,
   subtract `ACCESSORY_PENALTY`. This is what rejects "Makita battery
   holder" for a "Makita drill" search while still allowing it for a
   "Makita battery holder" search - *unless* the core match itself came
   from a **multi-word** strong/related synonym (e.g. "cordless drill",
   "drill driver", "impact driver" - two or more words, not just the
   bare family word). A multi-word match is unambiguous evidence the
   core product itself is genuinely what's being sold, so an accessory
   term elsewhere in the same title (e.g. "...Cordless Drill Driver Kit
   with Carrying Case and 2 Batteries") is read as an included bonus
   item, not the actual subject of the listing - the penalty stays fully
   in force for a *bare single-word* match (e.g. just "drill"), which is
   exactly the "Bosch Drill Bit Holder" case: "drill" there is only
   describing what kind of holder it is, not naming the product for
   sale. Found and fixed via this module's own regression-testing
   audit - see `_score_core_match`'s `unambiguous` return value and
   `tests/test_relevance.py`'s "kit/bundle" test cases.

A query that is *only* a recognized brand name, with no core product term
left after removing it (e.g. just "Makita"), is a special case: it's
relevant only if the listing actually mentions that brand
(`brand_only_query_not_mentioned` otherwise). A brand-neutral listing
being "not penalized" (point 2 above) only applies when there's still a
core product term to independently confirm the listing is on-topic - with
no core term at all, "doesn't mention a *different* brand" is not the
same claim as "is actually about this brand", and treating them as
equivalent would make a bare brand query match almost anything (found via
`core/persistence/cleanup.py`'s historical re-evaluation against real
production data - see CHANGELOG.md).

Final score is clamped to [0, 100]. `is_relevant` requires both a core
match and `score >= RELEVANCE_THRESHOLD`.
"""

from marketplace_alert.core.models.listing import Listing
from marketplace_alert.core.relevance import accessories, brands, families
from marketplace_alert.core.relevance.models import RelevanceEvaluation
from marketplace_alert.core.relevance.query import ParsedQuery, parse_query
from marketplace_alert.core.relevance.text import find_phrase_matches, tokenize

BRAND_MATCH_BONUS = 30
BRAND_CONFLICT_SCORE = 15
STRONG_CORE_MATCH_SCORE = 55
RELATED_CORE_MATCH_SCORE = 35
ACCESSORY_PENALTY = 45
RELEVANCE_THRESHOLD = 50
MIN_SCORE = 0
MAX_SCORE = 100


def evaluate_relevance(query: str, listing: Listing) -> RelevanceEvaluation:
    parsed_query = parse_query(query)
    listing_text = f"{listing.title} {listing.description or ''}"
    listing_tokens = tokenize(listing_text)
    matched_terms: list[str] = []

    listing_brand_matches = find_phrase_matches(listing_tokens, brands.brand_vocabulary())
    listing_brands = frozenset(listing_brand_matches.values())

    if parsed_query.brands:
        conflicting_brands = listing_brands - parsed_query.brands
        if conflicting_brands:
            return RelevanceEvaluation(
                is_relevant=False,
                score=BRAND_CONFLICT_SCORE,
                matched_terms=[],
                rejected_reason="brand_conflict",
            )
        matching_brands = listing_brands & parsed_query.brands
        if matching_brands:
            brand_score = BRAND_MATCH_BONUS
            matched_terms.extend(sorted(matching_brands))
        else:
            brand_score = 0  # brand-neutral listing - not penalized
    else:
        brand_score = 0

    # A query that's only a brand name (nothing left to check) has already
    # been proven not to conflict above - but it's only relevant if the
    # listing actually mentions that brand. A brand-neutral listing has no
    # core term to fall back on for a positive match here (unlike e.g.
    # "Makita drill" + "Cordless Drill Generic Brand", where "drill" is
    # still a real signal), so treating "doesn't conflict" as "is
    # relevant" would make a bare brand query match almost anything.
    if not parsed_query.core_tokens:
        if brand_score > 0:
            score = _clamp(brand_score + STRONG_CORE_MATCH_SCORE)
            return RelevanceEvaluation(is_relevant=True, score=score, matched_terms=matched_terms, rejected_reason=None)
        score = _clamp(brand_score)
        return RelevanceEvaluation(
            is_relevant=False, score=score, matched_terms=matched_terms, rejected_reason="brand_only_query_not_mentioned"
        )

    core_score, core_matched, core_matched_terms, core_match_unambiguous = _score_core_match(
        parsed_query, listing_tokens
    )
    matched_terms.extend(core_matched_terms)

    listing_accessory_matches = find_phrase_matches(listing_tokens, accessories.accessory_vocabulary())
    query_accessory_matches = find_phrase_matches(parsed_query.core_tokens, accessories.accessory_vocabulary())
    query_is_accessory_seeking = bool(query_accessory_matches)

    accessory_penalty = (
        ACCESSORY_PENALTY
        if listing_accessory_matches and not query_is_accessory_seeking and not core_match_unambiguous
        else 0
    )

    score = _clamp(brand_score + core_score - accessory_penalty)
    is_relevant = core_matched and score >= RELEVANCE_THRESHOLD

    rejected_reason = None
    if not is_relevant:
        if accessory_penalty > 0:
            rejected_reason = "accessory_without_core_product_match"
        elif not core_matched:
            rejected_reason = "no_core_product_match"
        else:
            rejected_reason = "low_relevance_score"

    return RelevanceEvaluation(is_relevant=is_relevant, score=score, matched_terms=matched_terms, rejected_reason=rejected_reason)


def _score_core_match(
    parsed_query: ParsedQuery, listing_tokens: list[str]
) -> tuple[int, bool, list[str], bool]:
    """Returns `(score, matched, matched_terms, unambiguous)`.

    `unambiguous` is True when the match is strong, unambiguous evidence
    that the core product itself - not merely an accessory that happens
    to mention it - is what the listing is for: specifically, a
    **multi-word** *family* synonym (e.g. "cordless drill", "impact
    driver" - two or more words). A single bare word (e.g. just "drill")
    is NOT unambiguous on its own - see `evaluate_relevance`'s
    accessory-penalty step for what this controls and why.

    Deliberately NOT extended to the lenient-fallback branch (an
    unregistered product category) even when multiple query tokens
    overlap: a multi-word *model name* (e.g. "Les Paul") appearing intact
    is exactly as likely in an accessory listing for that model ("Gibson
    Les Paul Guitar Stand") as in a listing for the model itself - unlike
    a curated family synonym, which was deliberately chosen to represent
    genuine product identity, "the query's own phrase matched completely"
    isn't evidence either way. Found via this module's own regression
    audit alongside the multi-word-family fix above - see
    `tests/test_relevance.py`.
    """
    family = families.find_family(parsed_query.core_tokens)
    if family is not None:
        strong_vocab = {tuple(tokenize(s)): s for s in family.strong_synonyms}
        strong_matches = find_phrase_matches(listing_tokens, strong_vocab)
        if strong_matches:
            unambiguous = any(len(phrase.split(" ")) > 1 for phrase in strong_matches)
            return STRONG_CORE_MATCH_SCORE, True, [family.core_term], unambiguous
        related_vocab = {tuple(tokenize(s)): s for s in family.related_synonyms}
        related_matches = find_phrase_matches(listing_tokens, related_vocab)
        if related_matches:
            unambiguous = any(len(phrase.split(" ")) > 1 for phrase in related_matches)
            return RELATED_CORE_MATCH_SCORE, True, sorted(set(related_matches.values())), unambiguous
        return 0, False, [], False

    query_accessory_matches = find_phrase_matches(parsed_query.core_tokens, accessories.accessory_vocabulary())
    if query_accessory_matches:
        # The query itself names an accessory (e.g. "battery holder") - the
        # listing must contain that same phrase to count as a core match.
        # Always "unambiguous": the accessory-penalty step already exempts
        # an accessory-seeking query independently, so this value is moot
        # for that step, but true either way - there's nothing ambiguous
        # about a match on the exact phrase the user asked for.
        target_phrase = max(query_accessory_matches, key=lambda phrase: len(phrase.split(" ")))
        if find_phrase_matches(listing_tokens, {tuple(tokenize(target_phrase)): target_phrase}):
            return STRONG_CORE_MATCH_SCORE, True, [target_phrase], True
        return 0, False, [], False

    # No registered family or accessory phrase applies - the query names a
    # product category this vocabulary doesn't know about. Deliberately
    # lenient: ANY shared token counts as a full strong match rather than a
    # score scaled by overlap fraction. A stricter/proportional fallback
    # would silently reject legitimate results for every unregistered
    # product category (this vocabulary only names tools/accessories
    # explicitly), which is worse than the brand-conflict/accessory-penalty
    # checks (applied independently, regardless of this branch) catching
    # the cases that actually matter.
    overlap = [token for token in parsed_query.core_tokens if token in listing_tokens]
    if overlap:
        return STRONG_CORE_MATCH_SCORE, True, overlap, False
    return 0, False, [], False


def _clamp(score: int) -> int:
    return max(MIN_SCORE, min(MAX_SCORE, score))
