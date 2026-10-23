"""Tests for the general relevance engine (v2) and its integration.

Layers:

1. Normalization / query parsing (Unicode, Hebrew, Russian, identifiers).
2. Scenario tests for `evaluate_relevance_v2` - the regression cases from the
   task that introduced it (Bosch drill vs. filters / dishwasher parts /
   drill bits, brand-only searches, Fender Stratocaster vs. cases, unknown
   brands and categories, model numbers, Hebrew / Russian / mixed queries,
   missing titles).
3. An old-vs-new comparison corpus: every row pins what the legacy engine
   decides AND what v2 decides, so any behaviour change between the two is
   explicit, reviewed and documented here - nothing changes silently.
4. Integration: engine selection (`legacy` default / `v2` / `shadow`), the
   outbox never receiving rejected listings, shadow mode never changing a
   decision and never logging a query.
5. Saved-search duplicate / overlap detection (advisory, never mutating).

No test touches a real marketplace, Telegram, Expo or the production DB.
"""

import logging

import pytest
from sqlalchemy import select

from marketplace_alert.config import settings
from marketplace_alert.core.auth.models import User
from marketplace_alert.core.models.listing import Listing
from marketplace_alert.core.persistence.models import DiscoveredListing, PendingNotification
from marketplace_alert.core.relevance import accessories, brands, evaluate_relevance, evaluate_relevance_v2, families
from marketplace_alert.core.relevance.engine_v2 import parse_query_v2
from marketplace_alert.core.relevance.normalization import (
    compact_windows,
    normalize_unicode,
    stem_token,
    tokenize_raw,
)
from marketplace_alert.core.relevance.service import filter_relevant_listings, resolve_engine
from marketplace_alert.core.saved_searches.duplicates import (
    RELATION_COVERED_BY_BROADER,
    RELATION_COVERS_NARROWER,
    RELATION_IDENTICAL,
    RELATION_OVERLAPPING_MARKETPLACES,
    WARNING_BRAND_ONLY,
    WARNING_NO_SEARCHABLE_TERMS,
    fingerprint,
    query_warnings,
    relationship,
)
from marketplace_alert.core.saved_searches.repository import SavedSearchRepository
from marketplace_alert.core.saved_searches.runner import SavedSearchRunner
from marketplace_alert.core.saved_searches.service import SavedSearchService
from marketplace_alert.core.scheduler.guard import SavedSearchRunGuard
from marketplace_alert.core.scheduler.scanner import BackgroundScanner


@pytest.fixture(autouse=True)
def _reset_vocabularies():
    brands.reset_to_defaults()
    families.reset_to_defaults()
    accessories.reset_to_defaults()
    logging.getLogger("marketplace_alert.core.relevance.service").disabled = False
    yield
    brands.reset_to_defaults()
    families.reset_to_defaults()
    accessories.reset_to_defaults()


def _l(title: str | None, description: str | None = None, external_id: str = "ext-1") -> Listing:
    return Listing(
        marketplace="mock",
        external_listing_id=external_id,
        title=title if title is not None else "",
        description=description,
        listing_url=f"https://example.com/mock/{external_id}",
    )


def _v2(query: str, title: str, description: str | None = None):
    return evaluate_relevance_v2(query, _l(title, description))


# =====================================================================
# 1. Normalization and query parsing
# =====================================================================


def test_normalize_unicode_handles_case_punctuation_and_whitespace() -> None:
    assert normalize_unicode("  BOSCH,, Drill!!  ") == "bosch drill"
    assert normalize_unicode(None) == ""
    assert normalize_unicode("   ") == ""


def test_normalize_unicode_strips_hebrew_niqqud_and_folds_final_letters() -> None:
    assert normalize_unicode("מַקְדֵּחָה") == normalize_unicode("מקדחה")
    assert normalize_unicode("מקדחים") == normalize_unicode("מקדחימ")  # final mem folded to regular mem


def test_normalize_unicode_keeps_non_latin_text_and_folds_yo() -> None:
    assert normalize_unicode("Дрель-ШУРУПОВЁРТ") == "дрель шуруповерт"
    assert "дрель" in normalize_unicode("ДРЕЛЬ Bosch")
    assert normalize_unicode("Café Ünïcode") == "cafe unicode"


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("drills", "drill"),
        ("batteries", "battery"),
        ("boxes", "box"),
        ("drilling", "drill"),
        ("running", "run"),
        ("дрель", "дрели"),
        ("дрель", "дрелью"),
        ("מקדחה", "המקדחה"),
        ("מקדחה", "מקדחות"),
    ],
)
def test_stemming_is_symmetric_for_common_inflections(a: str, b: str) -> None:
    assert stem_token(a) == stem_token(b)


@pytest.mark.parametrize("word", ["glass", "bus", "string", "bring", "gsb18v", "18v", "bosch"])
def test_stemming_leaves_unrelated_words_and_identifiers_alone(word: str) -> None:
    assert stem_token(word) == word


def test_compact_windows_join_adjacent_tokens() -> None:
    assert {"gsb18v55", "18v55", "gsb"} <= compact_windows(tokenize_raw("Bosch GSB 18V-55"))


def test_parse_query_separates_brand_from_product_terms() -> None:
    parsed = parse_query_v2("Bosch cordless drill")
    assert parsed.brands == {"bosch"}
    assert [t.display for t in parsed.terms] == ["cordless", "drill"]
    assert not parsed.is_brand_only


def test_parse_query_brand_only_is_broad_and_unknown_words_are_not_brands() -> None:
    assert parse_query_v2("Bosch").is_brand_only
    zorblax = parse_query_v2("Zorblax")
    assert zorblax.brands == frozenset()
    assert [t.display for t in zorblax.terms] == ["zorblax"]


def test_parse_query_recognizes_hebrew_and_russian_brand_transliterations() -> None:
    assert parse_query_v2("בוש").brands == {"bosch"}
    assert parse_query_v2("дрель Бош").brands == {"bosch"}


def test_parse_query_keeps_model_numbers_as_required_identifiers() -> None:
    parsed = parse_query_v2("Bosch GSB 18V-55")
    identifier = next(t for t in parsed.terms if t.is_identifier)
    assert identifier.compact == "18v55"
    assert identifier.must_match
    strong = parse_query_v2("SM-G991B").terms[0]
    assert strong.weight == 3


def test_parse_query_drops_stop_words_but_never_empties_a_query() -> None:
    assert [t.display for t in parse_query_v2("case for the guitar").terms] == ["case", "guitar"]
    assert [t.display for t in parse_query_v2("the").terms] == ["the"]


def test_parse_query_empty_input() -> None:
    assert parse_query_v2("   ").is_empty
    assert parse_query_v2(None).is_empty
    assert parse_query_v2("!!! ???").is_empty


# =====================================================================
# 2. Scenario / regression tests
# =====================================================================

# (query, title, expected_relevant)
BOSCH_DRILL_CASES = [
    # 1. vs diesel filter / 2. vs dishwasher parts / glue sticks
    ("Bosch drill", "Bosch Diesel Filter Fuel Filter 0450906", False),
    ("Bosch drill", "Bosch Dishwasher Spare Part Pump 00165261", False),
    ("Bosch drill", "Bosch Glue Sticks for Glue Gun 11mm", False),
    # 3. vs real drills, including word-order and inflection variants
    ("Bosch drill", "Bosch GSB 18V Cordless Drill Driver", True),
    ("Bosch drill", "Bosch Professional Hammer Drill GBH 2-26", True),
    ("Bosch drill", "Cordless Drill Bosch 18V", True),
    ("Bosch drill", "Bosch Drills Set of 3", True),
    ("Bosch drill", "Bosch Drilling Machine GBM 10 RE", True),
    ("Bosch drill", "Bosch Impact Driver GDR 18V", True),  # related family credit
    # accessories / parts of a drill are not drills
    ("Bosch drill", "Bosch Drill Bit Set 10pc", False),
    ("Bosch drill", "Bosch Drill Charger 18V", False),
    ("Bosch drill", "Bosch Drill Replacement Chuck Spare Parts", False),
    ("Bosch drill", "Battery for Bosch Drill", False),
    ("Bosch drill", "Diesel Filter for Bosch Drill Type 3", False),
    ("Bosch drill", "For Sale: Bosch Cordless Drill", True),  # "for sale" is not a compatibility marker
    # included extras do not make a drill an accessory
    ("Bosch drill", "Bosch Cordless Drill Driver Kit with Case and 2 Batteries", True),
    ("Bosch drill", "Bosch Cordless Drill 18V 2 Batteries and Charger", True),
    # brand conflict
    ("Bosch drill", "Makita Cordless Drill", False),
    # brand-neutral listing with a real drill is not penalized
    ("Bosch drill", "Cordless Drill 18V Brushless", True),
    # 4. drill bits
    ("Bosch drill bits", "Bosch Drill Bit Set 10pc", True),
    ("Bosch drill bits", "Bosch Titanium Drill Bits 19 pcs", True),
    ("Bosch drill bits", "Bosch Cordless Drill", False),
    ("Bosch drill bits", "Bosch Diesel Filter", False),
]


@pytest.mark.parametrize(("query", "title", "expected"), BOSCH_DRILL_CASES)
def test_bosch_drill_family_scenarios(query: str, title: str, expected: bool) -> None:
    result = _v2(query, title)
    assert result.is_relevant is expected, (query, title, result)


# 5. brand-only searches are intentionally broad
@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Bosch Cordless Drill", True),
        ("Bosch Diesel Filter", True),
        ("Bosch Dishwasher Spare Part", True),
        ("Bosch Glue Gun", True),
        ("Makita Drill", False),
        ("Generic Cordless Drill", False),
    ],
)
def test_brand_only_search_includes_every_product_type_of_that_brand(title: str, expected: bool) -> None:
    assert _v2("Bosch", title).is_relevant is expected


def test_brand_only_search_accepts_a_description_only_mention_with_a_lower_score() -> None:
    in_title = _v2("Bosch", "Bosch Cordless Drill")
    in_description = _v2("Bosch", "Cordless Drill 18V", "Genuine Bosch professional tool")
    assert in_description.is_relevant
    assert in_description.score < in_title.score


# 6 & 7. Fender Stratocaster vs cases
FENDER_CASES = [
    ("Fender Stratocaster", "Fender Stratocaster Electric Guitar", True),
    ("Fender Stratocaster", "Fender American Standard Stratocaster w/ Case", True),
    ("Fender Stratocaster", "Fender Stratocaster Hard Case", False),
    ("Fender Stratocaster", "Stratocaster Guitar Gig Bag", False),
    ("Fender Stratocaster", "Gibson Les Paul Standard", False),
    ("Fender Stratocaster", "Squier Stratocaster Affinity", False),
    ("Fender Stratocaster case", "Fender Stratocaster Hard Case", True),
    ("Fender Stratocaster case", "Case for Fender Stratocaster", True),
    ("Fender Stratocaster case", "Fender Stratocaster Electric Guitar", False),
    ("Fender Stratocaster case", "Generic Guitar Case", False),
]


@pytest.mark.parametrize(("query", "title", "expected"), FENDER_CASES)
def test_fender_stratocaster_versus_cases(query: str, title: str, expected: bool) -> None:
    assert _v2(query, title).is_relevant is expected, (query, title)


# 8. unknown brands and unknown product categories (no registration needed)
@pytest.mark.parametrize(
    ("query", "title", "expected"),
    [
        ("Zorblax", "Zorblax Frobnicator Mk2", True),
        ("Zorblax", "Frobnicator Mk2", False),
        ("Acme widget", "Acme Widget Pro", True),
        ("Acme widget", "Widget Pro by Acme", True),
        ("Acme widget", "Acme Gadget", False),
        ("Acme widget", "Widget by Zeta", False),
        ("red leather jacket", "Jacket Leather Red Mens Large", True),
        ("red leather jacket", "Blue Leather Jacket", False),
        ("vintage ceramic teapot", "Vintage Teapot Ceramic Floral", True),
        ("vintage ceramic teapot", "Vintage Ceramic Cup", False),
    ],
)
def test_unknown_brands_and_categories_are_matched_lexically(query: str, title: str, expected: bool) -> None:
    assert _v2(query, title).is_relevant is expected, (query, title)


def test_unknown_word_is_never_promoted_to_a_brand() -> None:
    # "Acme" is not a known brand, so another unknown brand name in the title is not a "brand conflict".
    assert _v2("Acme widget", "Acme Widget by Zeta Corp").is_relevant


# 9. model numbers and mixed alphanumeric identifiers
@pytest.mark.parametrize(
    ("query", "title", "expected"),
    [
        ("GSB 18V-55", "Bosch GSB18V-55 Professional", True),
        ("GSB 18V-55", "Bosch GSB 18V 55 Drill", True),
        ("GSB 18V-55", "Bosch GSB 18V-50", False),
        ("SM-G991B", "Samsung Galaxy S21 SM-G991B 128GB", True),
        ("SM-G991B", "Samsung SM-G991U", False),
        ("SM-G991B", "Samsung Galaxy S21", False),
        ("iPhone 13", "Apple iPhone 13 128GB", True),
        ("iPhone 13", "Apple iPhone13 Blue", True),
        ("iPhone 13", "Apple iPhone 12", False),
        ("sony wh-1000xm4 headphones", "Sony WH-1000XM4 Wireless Headphones", True),
        ("sony wh-1000xm4 headphones", "Sony WH-1000XM5 Headphones", False),
        ("Bosch GSB 18V-55", "Bosch GSB 18V-55 Professional Drill", True),
        ("Bosch GSB 18V-55", "Makita GSB 18V-55", False),
        ("Makita DTD153", "Makita DTD153Z Impact Driver", False),
        ("Makita DTD153", "Makita DTD153 Impact Driver", True),
    ],
)
def test_model_numbers_must_match_exactly_but_ignore_spacing_and_punctuation(
    query: str, title: str, expected: bool
) -> None:
    result = _v2(query, title)
    assert result.is_relevant is expected, (query, title, result)


def test_model_number_mismatch_has_a_distinct_machine_readable_reason() -> None:
    assert _v2("SM-G991B", "Samsung SM-G991U").rejected_reason == "missing_model_identifier"


def test_initialisms_of_adjacent_words_match() -> None:
    assert _v2("Nintendo Game Boy Advance SP", "Nintendo GBA SP Console").is_relevant
    assert not _v2("Nintendo Game Boy Advance SP", "SP Repair Tool Kit").is_relevant


# 10. Hebrew, 11. Russian, 12. mixed
@pytest.mark.parametrize(
    ("query", "title", "expected"),
    [
        ("מקדחה בוש", "בוש מקדחה נטענת", True),
        ("מקדחה בוש", "המקדחה של בוש", True),
        ("מקדחה בוש", "בוש מסנן סולר", False),
        ("מקדחה", "מקדחות חשמליות", True),
        ("מקדחה", "מסנן סולר", False),
        ("מקדחה בוש", "מקדחה מקיטה", False),  # Hebrew brand conflict via transliteration
        ("מַקְדֵּחָה", "מקדחה", True),  # niqqud in the query
        ("дрель Bosch", "Дрель-шуруповерт Bosch GSR 18V", True),
        ("дрель Bosch", "Bosch фильтр дизельный", False),
        ("дрель бош", "Бош дрели аккумуляторные", True),
        ("дрель", "Чехол для дрели", False),  # accessory, not a drill
        ("чехол для дрели", "Чехол для дрели Bosch", True),
        ("дрель Bosch", "Дрель Makita 18V", False),
        ("מקדחה Bosch", "Bosch מקדחה נטענת 18V", True),  # mixed Hebrew + Latin
        ("дрель Bosch", "Bosch Cordless Drill", False),  # no cross-language translation (documented limitation)
    ],
)
def test_hebrew_russian_and_mixed_language_queries(query: str, title: str, expected: bool) -> None:
    assert _v2(query, title).is_relevant is expected, (query, title)


def test_non_latin_query_is_never_silently_discarded() -> None:
    parsed = parse_query_v2("מקדחה")
    assert [t.display for t in parsed.terms] == ["מקדחה"]
    assert not _v2("מקדחה", "Bosch Cordless Drill").is_relevant


# 13. missing / incomplete titles
@pytest.mark.parametrize("title", ["", "   ", "!!!", None])
def test_missing_or_blank_title_is_rejected_with_a_reason(title) -> None:
    result = evaluate_relevance_v2("Bosch drill", _l(title))
    assert not result.is_relevant
    assert result.rejected_reason == "missing_title"


def test_a_description_alone_cannot_make_a_blank_title_relevant() -> None:
    assert not evaluate_relevance_v2("Bosch drill", _l("", "Bosch cordless drill 18V")).is_relevant


def test_core_terms_in_the_description_do_not_count_only_the_title() -> None:
    result = _v2("Bosch drill", "Bosch HDC200 Dust Extractor", "Works with any drill")
    assert not result.is_relevant


def test_blank_query_is_rejected_with_a_reason() -> None:
    result = evaluate_relevance_v2("  ", _l("Bosch drill"))
    assert not result.is_relevant
    assert result.rejected_reason == "empty_query"


# explainability / configurability
def test_result_is_explainable_with_a_score_breakdown() -> None:
    result = _v2("Bosch drill", "Bosch GSB Cordless Drill")
    assert result.is_relevant
    assert result.breakdown["product"] == 70
    assert result.breakdown["brand"] == 15
    assert result.breakdown["accessory"] == 0
    assert result.score == sum(result.breakdown.values())
    assert "bosch" in result.matched_terms and "drill" in result.matched_terms


def test_rejections_carry_machine_readable_reasons_and_missing_terms() -> None:
    missing = _v2("Bosch drill", "Bosch Diesel Filter")
    assert (missing.rejected_reason, missing.missing_terms) == ("missing_required_terms", ("drill",))
    assert _v2("Bosch drill", "Makita Drill").rejected_reason == "brand_conflict"
    assert _v2("Bosch drill", "Bosch Drill Bit Set").rejected_reason == "accessory_mismatch"
    assert _v2("Bosch drill", "Charger for Bosch Drill").rejected_reason == "accessory_mismatch"
    assert _v2("Bosch", "Generic Drill").rejected_reason == "brand_only_not_mentioned"


def test_accept_threshold_is_configurable() -> None:
    listing = _l("Bosch Impact Driver GDR")  # only a related-family match for "drill"
    assert evaluate_relevance_v2("Bosch drill", listing, min_score=50).is_relevant
    assert not evaluate_relevance_v2("Bosch drill", listing, min_score=90).is_relevant


def test_long_queries_tolerate_one_missing_plain_word_but_never_a_number() -> None:
    assert _v2("bosch professional cordless brushless drill driver", "Bosch Cordless Brushless Drill Driver").is_relevant
    assert not _v2("bosch cordless drill 18v", "Bosch Cordless Drill 12V").is_relevant


def test_custom_registered_brand_is_honoured() -> None:
    brands.register_brand("zorblax")
    parsed = parse_query_v2("Zorblax widget")
    assert parsed.brands == {"zorblax"}
    assert _v2("Zorblax widget", "Zorblax Widget Pro").is_relevant
    assert _v2("Zorblax widget", "Makita Widget").rejected_reason == "brand_conflict"


# =====================================================================
# 3. Old versus new: an explicit, reviewed comparison corpus
# =====================================================================
# (query, title, legacy_relevant, v2_relevant)
COMPARISON_CORPUS = [
    # identical decisions - the core of the existing behaviour is preserved
    ("Bosch drill", "Bosch GSB 18V Cordless Drill Driver", True, True),
    ("Bosch drill", "Bosch Professional Hammer Drill GBH 2-26", True, True),
    ("Bosch drill", "Bosch Diesel Filter Fuel Filter 0450906", False, False),
    ("Bosch drill", "Bosch Dishwasher Spare Part Pump 00165261", False, False),
    ("Bosch drill", "Bosch Glue Sticks for Glue Gun 11mm", False, False),
    ("Bosch drill", "Makita Cordless Drill", False, False),
    ("Bosch drill", "Bosch Drill Charger 18V", False, False),
    ("Bosch drill", "Bosch Impact Driver GDR 18V", True, True),
    ("Bosch drill", "Cordless Drill 18V Brushless", True, True),
    ("Makita drill", "Makita Battery Holder Wall Mount", False, False),
    ("Makita drill", "DeWalt 20V Drill DCD771C2", False, False),
    ("Makita drill", "Makita Cordless Drill 18V", True, True),
    ("Bosch", "Bosch Diesel Filter", True, True),  # brand-only stays broad in both
    ("Bosch", "Makita Drill", False, False),
    ("Fender Stratocaster", "Fender Stratocaster Electric Guitar", True, True),
    ("Fender Stratocaster", "Fender American Standard Stratocaster w/ Case", True, True),
    ("Fender Stratocaster", "Fender Stratocaster Hard Case", False, False),
    ("Fender Stratocaster", "Gibson Les Paul", False, False),
    ("Fender Stratocaster case", "Fender Stratocaster Hard Case", True, True),
    ("Acme widget", "Acme Widget Pro 3000", True, True),
    ("Acme widget", "Acme Gadget", False, False),
    ("iPhone 13", "Apple iPhone 13 128GB", True, True),
    ("iPhone 13", "Apple iPhone 12", False, False),
    ("дрель Bosch", "Дрель-шуруповерт Bosch GSR", True, True),
    ("дрель Bosch", "Bosch фильтр дизельный", False, False),
    ("מקדחה Bosch", "Bosch מקדחה נטענת", True, True),
    ("מקדחה Bosch", "Bosch Diesel Filter", False, False),
    ("red leather jacket", "Jacket Leather Red Mens", True, True),
    ("Nintendo Game Boy Advance SP", "SP Repair Tool Kit", False, False),
    ("Nintendo Game Boy Advance SP", "Nintendo GBA SP Console", True, True),
    ("Makita impact driver", "Makita DTD153 Impact Driver", True, True),
    ("Makita impact driver", "Makita Impact Wrench", False, False),
    # intended v2 stricter results (legacy false positives)
    ("Bosch drill", "Bosch Drill Bit Set 10pc", True, False),
    ("Bosch drill", "Bosch Drill Replacement Chuck Spare Parts", True, False),
    ("Bosch drill bits", "Bosch Cordless Drill", True, False),
    ("GSB 18V-55", "Bosch GSB 18V-50", True, False),
    ("sony wh-1000xm4 headphones", "Sony WH-1000XM5 Headphones", True, False),
    ("Fender Stratocaster case", "Generic Guitar Case", True, False),
    # intended v2 more permissive results (legacy false negatives)
    ("Bosch drill", "Bosch Drilling Machine GBM 10 RE", False, True),
    ("GSB 18V-55", "Bosch GSB18V-55 Professional", False, True),
    ("מקדחה בוש", "המקדחה של בוש", False, True),
    ("дрель бош", "Бош дрели аккумуляторные", False, True),
    # known v2 regression vs legacy: partial-word lenience dropped (documented limitation)
    ("red leather jacket", "Red Jacket", True, False),
]


@pytest.mark.parametrize(("query", "title", "legacy", "new"), COMPARISON_CORPUS)
def test_comparison_corpus_pins_both_engines(query: str, title: str, legacy: bool, new: bool) -> None:
    assert evaluate_relevance(query, _l(title)).is_relevant is legacy, ("legacy", query, title)
    assert evaluate_relevance_v2(query, _l(title)).is_relevant is new, ("v2", query, title)


def test_comparison_corpus_summary_is_what_the_report_states() -> None:
    same = sum(1 for _, _, a, b in COMPARISON_CORPUS if a == b)
    stricter = sum(1 for _, _, a, b in COMPARISON_CORPUS if a and not b)
    more_permissive = sum(1 for _, _, a, b in COMPARISON_CORPUS if not a and b)
    assert (same, stricter, more_permissive) == (32, 7, 4)


def test_legitimate_listings_accepted_by_both_engines_stay_accepted() -> None:
    legitimate = [(q, t) for q, t, a, b in COMPARISON_CORPUS if a and b]
    assert legitimate
    for query, title in legitimate:
        assert evaluate_relevance_v2(query, _l(title)).is_relevant, (query, title)


# =====================================================================
# 4. Integration: engine selection, outbox, shadow mode
# =====================================================================


def test_default_engine_is_legacy_so_deploying_changes_nothing() -> None:
    assert settings.relevance_engine == "legacy"
    assert resolve_engine() == "legacy"
    result = filter_relevant_listings(
        query="Bosch drill", listings=[_l("Bosch Drill Bit Set 10pc")], marketplace="mock"
    )
    assert result.relevant_count == 1  # legacy behaviour, unchanged


@pytest.mark.parametrize(
    ("configured", "expected"),
    [("v2", "v2"), ("V2 ", "v2"), ("shadow", "shadow"), ("legacy", "legacy"), ("", "legacy"), ("nonsense", "legacy")],
)
def test_resolve_engine_falls_back_to_legacy_for_anything_unrecognized(configured: str, expected: str) -> None:
    assert resolve_engine(configured) == expected


def test_v2_engine_filters_through_the_shared_entry_point() -> None:
    listings = [_l("Bosch Cordless Drill", external_id="a"), _l("Bosch Diesel Filter", external_id="b")]
    result = filter_relevant_listings(query="Bosch drill", listings=listings, marketplace="mock", engine="v2")
    assert [x.external_listing_id for x in result.relevant_listings] == ["a"]
    assert (result.raw_count, result.relevant_count, result.rejected_count) == (2, 1, 1)


def test_v2_threshold_comes_from_settings_unless_overridden(monkeypatch) -> None:
    listings = [_l("Bosch Impact Driver GDR")]
    assert filter_relevant_listings(query="Bosch drill", listings=listings, marketplace="mock", engine="v2").relevant_count == 1
    monkeypatch.setattr(settings, "relevance_min_score", 90)
    assert filter_relevant_listings(query="Bosch drill", listings=listings, marketplace="mock", engine="v2").relevant_count == 0
    assert filter_relevant_listings(
        query="Bosch drill", listings=listings, marketplace="mock", engine="v2", min_score=50
    ).relevant_count == 1


class FakeConnector:
    def __init__(self, listings: list[Listing]) -> None:
        self._listings = listings

    def search(self, query: str, filters: dict | None = None) -> list[Listing]:
        return self._listings


def _outbox_listing_ids(session) -> list[str]:
    stmt = (
        select(DiscoveredListing.external_listing_id)
        .join(PendingNotification, PendingNotification.discovered_listing_id == DiscoveredListing.id)
        .order_by(PendingNotification.id)
    )
    return list(session.execute(stmt).scalars().all())


def _scan(session_factory, *, query: str, listings: list[Listing]) -> None:
    setup = session_factory()
    SavedSearchRepository(setup).create(query=query, marketplaces=["good"], scan_interval_seconds=60, is_active=True)
    setup.commit()
    setup.close()
    runner = SavedSearchRunner(resolve_connector=lambda name: FakeConnector(listings))
    BackgroundScanner(session_factory=session_factory, runner=runner, run_guard=SavedSearchRunGuard()).run_due_searches()


_BOSCH_LISTINGS = [
    _l("Bosch Cordless Drill 18V", external_id="drill"),
    _l("Bosch Diesel Filter", external_id="filter"),
    _l("Bosch Dishwasher Spare Part", external_id="dishwasher"),
    _l("Bosch Glue Sticks", external_id="glue"),
    _l("Bosch Drill Bit Set 10pc", external_id="bits"),
]


def test_v2_rejected_listings_never_reach_the_database_or_the_outbox(session_factory, monkeypatch) -> None:
    monkeypatch.setattr(settings, "relevance_engine", "v2")
    _scan(session_factory, query="Bosch drill", listings=_BOSCH_LISTINGS)

    verify = session_factory()
    assert _outbox_listing_ids(verify) == ["drill"]
    assert {r.external_listing_id for r in verify.query(DiscoveredListing).all()} == {"drill"}
    verify.close()


def test_legacy_engine_remains_the_default_in_the_scan_path(session_factory) -> None:
    _scan(session_factory, query="Bosch drill", listings=_BOSCH_LISTINGS)

    verify = session_factory()
    assert _outbox_listing_ids(verify) == ["drill", "bits"]  # legacy still accepts the drill-bit set
    verify.close()


def test_shadow_mode_uses_legacy_decisions_and_logs_disagreements_without_the_query(caplog) -> None:
    query = "Bosch drill secretword"
    with caplog.at_level(logging.INFO, logger="marketplace_alert.core.relevance.service"):
        result = filter_relevant_listings(
            query="Bosch drill",
            listings=[_l("Bosch Drill Bit Set 10pc", external_id="bits"), _l("Bosch Cordless Drill", external_id="ok")],
            marketplace="mock",
            saved_search_id=7,
            engine="shadow",
        )
    assert {x.external_listing_id for x in result.relevant_listings} == {"bits", "ok"}  # legacy decides
    shadow = [r.getMessage() for r in caplog.records if "Relevance shadow detail" in r.getMessage()]
    assert len(shadow) == 1
    assert "saved search 7" in shadow[0] and "bits" in shadow[0] and "legacy=accept" in shadow[0] and "v2=reject" in shadow[0]
    assert "Bosch" not in shadow[0] and "drill" not in shadow[0].lower().replace("accessory_mismatch", "")
    assert query not in caplog.text


def test_shadow_mode_survives_a_v2_failure(monkeypatch) -> None:
    from marketplace_alert.core.relevance import service

    def boom(*args, **kwargs):
        raise RuntimeError("v2 exploded")

    monkeypatch.setattr(service, "evaluate_relevance_v2", boom)
    result = filter_relevant_listings(
        query="Bosch drill", listings=[_l("Bosch Cordless Drill")], marketplace="mock", engine="shadow"
    )
    assert result.relevant_count == 1


def test_rejection_log_never_contains_the_query_in_either_engine(caplog) -> None:
    for engine in ("legacy", "v2"):
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="marketplace_alert.core.relevance.service"):
            filter_relevant_listings(
                query="Bosch drill", listings=[_l("Bosch Diesel Filter")], marketplace="mock", saved_search_id=3, engine=engine
            )
        assert "Bosch drill" not in caplog.text
        assert "saved search 3" in caplog.text


# =====================================================================
# 5. Saved-search duplicate / overlap detection
# =====================================================================


def test_fingerprint_is_order_inflection_case_and_punctuation_insensitive() -> None:
    assert fingerprint("Bosch drill").keys == fingerprint("DRILLS, bosch!").keys
    assert fingerprint("בוש מקדחה").keys == fingerprint("מקדחה  Bosch").keys
    assert fingerprint("Bosch drill").keys != fingerprint("Bosch drill bits").keys


MKT_A = frozenset({"ebay"})
MKT_AB = frozenset({"ebay", "etsy"})
MKT_C = frozenset({"reverb"})


def test_relationship_classification() -> None:
    drill = fingerprint("Bosch drill")
    assert relationship(drill, MKT_A, fingerprint("drill bosch"), MKT_A) == RELATION_IDENTICAL
    assert relationship(drill, MKT_A, fingerprint("drill bosch"), MKT_AB) == RELATION_OVERLAPPING_MARKETPLACES
    assert relationship(drill, MKT_A, fingerprint("Bosch"), MKT_AB) == RELATION_COVERED_BY_BROADER
    assert relationship(drill, MKT_A, fingerprint("Bosch cordless drill"), MKT_A) == RELATION_COVERS_NARROWER
    assert relationship(drill, MKT_A, fingerprint("drill bosch"), MKT_C) is None  # no marketplace in common
    assert relationship(drill, MKT_A, fingerprint("Makita drill"), MKT_A) is None  # different brand
    assert relationship(drill, MKT_A, fingerprint("Bosch hammer"), MKT_A) is None  # unrelated


def test_query_warnings() -> None:
    assert query_warnings("Bosch") == [WARNING_BRAND_ONLY]
    assert query_warnings("!!!") == [WARNING_NO_SEARCHABLE_TERMS]
    assert query_warnings("Bosch drill") == []


def _user(session, email: str) -> User:
    user = User(email=email, password_hash="irrelevant-hash")
    session.add(user)
    session.commit()
    return user


def _owned(session, user: User, query: str, marketplaces: list[str], *, is_active: bool = True):
    saved = SavedSearchRepository(session).create(
        query=query, marketplaces=marketplaces, scan_interval_seconds=300, is_active=is_active, user_id=user.id
    )
    session.commit()
    return saved


def _service(session) -> SavedSearchService:
    return SavedSearchService(session, is_marketplace_supported=lambda _n: True)


def test_service_reports_only_this_users_overlapping_searches(db_session) -> None:
    me, other = _user(db_session, "me@example.com"), _user(db_session, "other@example.com")
    identical = _owned(db_session, me, "drill Bosch", ["ebay"])
    broader = _owned(db_session, me, "Bosch", ["ebay", "etsy"], is_active=False)
    _owned(db_session, me, "Makita drill", ["ebay"])
    _owned(db_session, other, "Bosch drill", ["ebay"])  # someone else's: never reported

    check = _service(db_session).check_duplicates_owned(user_id=me.id, query="Bosch drill", marketplaces=["ebay"])

    found = {s.saved_search_id: (s.relationship, s.is_active) for s in check.similar}
    assert found == {identical.id: (RELATION_IDENTICAL, True), broader.id: (RELATION_COVERED_BY_BROADER, False)}
    assert check.warnings == []


def test_service_excludes_the_search_being_edited(db_session) -> None:
    me = _user(db_session, "me@example.com")
    editing = _owned(db_session, me, "Bosch drill", ["ebay"])
    check = _service(db_session).check_duplicates_owned(
        user_id=me.id, query="Bosch drill", marketplaces=["ebay"], exclude_saved_search_id=editing.id
    )
    assert check.similar == []


def test_duplicate_check_never_writes(db_session) -> None:
    me = _user(db_session, "me@example.com")
    _owned(db_session, me, "Bosch drill", ["ebay"])
    def snapshot():
        return [
            (s.id, s.query, tuple(s.marketplaces), s.updated_at.replace(tzinfo=None))
            for s in SavedSearchRepository(db_session).list_all()
        ]

    before = snapshot()
    _service(db_session).check_duplicates_owned(user_id=me.id, query="Bosch drill", marketplaces=["ebay"])
    db_session.expire_all()
    assert snapshot() == before


def _signup(client, email: str) -> dict:
    body = client.post("/api/v1/auth/signup", json={"email": email, "password": "a-strong-password"}).json()
    return {"Authorization": f"Bearer {body['tokens']['access_token']}"}


def _create_api(client, headers, query, marketplaces=("mock",)):
    return client.post(
        "/api/v1/saved-searches",
        headers=headers,
        json={"query": query, "marketplaces": list(marketplaces), "scan_interval_seconds": 60},
    )


def test_api_check_duplicates_reports_overlaps_and_warnings(client) -> None:
    headers = _signup(client, "dups@example.com")
    existing = _create_api(client, headers, "Bosch drill", ["mock"]).json()

    response = client.post(
        "/api/v1/saved-searches/check-duplicates",
        headers=headers,
        json={"query": "drill  BOSCH", "marketplaces": ["mock"]},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["similar"] == [
        {
            "saved_search_id": existing["id"],
            "query": "Bosch drill",
            "marketplaces": ["mock"],
            "is_active": True,
            "relationship": "identical",
        }
    ]
    assert body["warnings"] == []

    broad = client.post(
        "/api/v1/saved-searches/check-duplicates", headers=headers, json={"query": "Bosch", "marketplaces": ["mock"]}
    ).json()
    assert broad["warnings"] == ["brand_only_query"]
    assert broad["similar"][0]["relationship"] == "covers_narrower_search"


def test_api_check_duplicates_is_per_user_and_requires_authentication(client) -> None:
    mine, theirs = _signup(client, "mine@example.com"), _signup(client, "theirs@example.com")
    _create_api(client, theirs, "Bosch drill")

    assert (
        client.post(
            "/api/v1/saved-searches/check-duplicates", headers=mine, json={"query": "Bosch drill", "marketplaces": ["mock"]}
        ).json()["similar"]
        == []
    )
    unauthenticated = client.post(
        "/api/v1/saved-searches/check-duplicates", json={"query": "Bosch drill", "marketplaces": ["mock"]}
    )
    assert unauthenticated.status_code == 401


def test_api_check_duplicates_validates_input(client) -> None:
    headers = _signup(client, "validate@example.com")
    for body in ({"query": "  ", "marketplaces": ["mock"]}, {"query": "x", "marketplaces": []}):
        assert (
            client.post("/api/v1/saved-searches/check-duplicates", headers=headers, json=body).status_code == 422
        )


def test_creating_a_duplicate_is_still_allowed_and_nothing_is_merged(client) -> None:
    """Backward compatible: the advisory endpoint never changes create/update."""
    headers = _signup(client, "allowed@example.com")
    first = _create_api(client, headers, "Bosch drill")
    second = _create_api(client, headers, "Bosch drill")
    assert (first.status_code, second.status_code) == (201, 201)
    assert first.json()["id"] != second.json()["id"]
    assert len(client.get("/api/v1/saved-searches", headers=headers).json()) == 2


def test_existing_saved_search_response_shape_is_unchanged(client) -> None:
    headers = _signup(client, "shape@example.com")
    body = _create_api(client, headers, "Bosch drill").json()
    assert set(body) == {
        "id", "query", "marketplaces", "scan_interval_seconds", "is_active", "created_at", "updated_at", "last_scanned_at"
    }  # fmt: skip
