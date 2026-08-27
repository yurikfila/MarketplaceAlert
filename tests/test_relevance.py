"""Tests for the relevance filtering layer (`marketplace_alert/core/relevance/`).

Three layers, matching the module's own structure:

1. **Unit tests** for the building blocks (`text`, `brands`, `families`,
   `accessories`, `query`) - normalization, phrase matching, vocabulary
   extensibility.
2. **Scenario tests** for `evaluate_relevance` - every scenario named in
   the task that introduced this module (Makita/Bosch drill searches,
   accessory rejection, brand conflicts, bare no-brand queries), plus
   punctuation/case/whitespace robustness.
3. **Integration tests** proving relevance filtering is wired into every
   shared path - the background scheduler, both Run Now endpoints
   (legacy and mobile), and the legacy `/scan` endpoint - and that a
   rejected listing is never persisted, never notified, and never marked
   "already seen" (since it was never persisted in the first place).

Never touches a real marketplace or Telegram - integration tests use the
same `FakeConnector` pattern as `tests/test_saved_search_scheduler.py`, or
monkeypatch the mock connector, exactly like the rest of the suite.
"""

import logging

import pytest

from marketplace_alert.core.models.listing import Listing
from marketplace_alert.core.notifications.base import NotificationProvider
from marketplace_alert.core.notifications.service import NotificationService
from marketplace_alert.core.persistence.models import DiscoveredListing
from marketplace_alert.core.relevance import accessories, brands, evaluate_relevance, families
from marketplace_alert.core.relevance.query import parse_query
from marketplace_alert.core.relevance.service import filter_relevant_listings
from marketplace_alert.core.relevance.text import find_phrase_match_positions, find_phrase_matches, normalize_text, tokenize
from marketplace_alert.core.saved_searches.repository import SavedSearchRepository
from marketplace_alert.core.saved_searches.runner import SavedSearchRunner
from marketplace_alert.core.scheduler.guard import SavedSearchRunGuard
from marketplace_alert.core.scheduler.scanner import BackgroundScanner


@pytest.fixture(autouse=True)
def _reset_relevance_vocabularies():
    """Any test that registers a custom brand/family/accessory term must
    not leak it into other tests - reset to the default catalog before and
    after every test in this module."""
    brands.reset_to_defaults()
    families.reset_to_defaults()
    accessories.reset_to_defaults()
    # Alembic's Config, used by tests/test_alembic_migrations.py, calls
    # logging.config.fileConfig(alembic.ini) which - with Python logging's
    # default disable_existing_loggers=True - disables every logger not
    # explicitly listed in that ini (root/sqlalchemy/alembic only) for the
    # rest of the process. If that test module happens to run before this
    # one in the same session, this module's logger would otherwise go
    # silent with no error, just zero records ever reaching caplog.
    logging.getLogger("marketplace_alert.core.relevance.service").disabled = False
    yield
    brands.reset_to_defaults()
    families.reset_to_defaults()
    accessories.reset_to_defaults()


def _listing(title: str, *, description: str | None = None, external_id: str = "ext-1", marketplace: str = "mock") -> Listing:
    return Listing(
        marketplace=marketplace,
        external_listing_id=external_id,
        title=title,
        description=description,
        listing_url=f"https://example.com/{marketplace}/{external_id}",
    )


# =====================================================================
# text.py - normalization and phrase matching
# =====================================================================


def test_normalize_text_lowercases_strips_punctuation_and_collapses_whitespace() -> None:
    assert normalize_text("  MAKITA,, Drill!!  ") == "makita drill"
    assert normalize_text("Black+Decker / Drill") == "black decker drill"
    assert normalize_text("porter-cable") == "porter cable"


def test_tokenize_singularizes_simple_plurals() -> None:
    assert tokenize("batteries") == ["battery"]
    assert tokenize("holders") == ["holder"]
    assert tokenize("drills") == ["drill"]
    # short/already-plural-looking words are left alone rather than mangled
    assert tokenize("glass") == ["glass"]


def test_tokenize_empty_or_blank_string_returns_empty_list() -> None:
    assert tokenize("") == []
    assert tokenize("   ") == []


def test_find_phrase_matches_matches_contiguous_multi_word_phrases() -> None:
    vocabulary = {("hammer", "drill"): "hammer_drill", ("drill",): "drill"}
    tokens = tokenize("makita hammer drill driver")
    matches = find_phrase_matches(tokens, vocabulary)
    assert matches == {"hammer drill": "hammer_drill", "drill": "drill"}


def test_find_phrase_matches_returns_empty_when_nothing_matches() -> None:
    vocabulary = {("impact", "driver"): "impact_driver"}
    assert find_phrase_matches(tokenize("makita cordless drill"), vocabulary) == {}


def test_find_phrase_match_positions_returns_start_and_end_indices() -> None:
    tokens = tokenize("fender stratocaster with pau ferro neck")
    matches = find_phrase_match_positions(tokens, {("neck",): "neck"})
    assert matches == [(5, 6, "neck")]


def test_find_phrase_match_positions_returns_empty_when_nothing_matches() -> None:
    assert find_phrase_match_positions(tokenize("makita cordless drill"), {("impact", "driver"): "impact_driver"}) == []


# =====================================================================
# brands.py - extensible brand vocabulary
# =====================================================================


def test_default_brands_include_common_power_tool_brands() -> None:
    known = brands.known_brands()
    for expected in ("makita", "bosch", "dewalt", "milwaukee"):
        assert expected in known


def test_register_brand_extends_vocabulary_without_hardcoding() -> None:
    """The brand list is a maintainable registry, not a fixed set of four
    hard-coded names - registering a brand nobody shipped by default must
    immediately participate in matching."""
    brands.register_brand("acme tools", aliases=["acme"])
    tokens = tokenize("Acme Cordless Drill")
    matches = find_phrase_matches(tokens, brands.brand_vocabulary())
    assert "acme tools" in matches.values()


def test_reset_to_defaults_removes_custom_registrations() -> None:
    brands.register_brand("acme tools")
    brands.reset_to_defaults()
    assert "acme tools" not in brands.known_brands()
    assert "makita" in brands.known_brands()


# =====================================================================
# families.py - configurable product-family synonyms
# =====================================================================


def test_find_family_matches_by_core_term_token() -> None:
    family = families.find_family(["drill"])
    assert family is not None
    assert family.core_term == "drill"
    assert "hammer drill" in family.strong_synonyms


def test_find_family_matches_by_full_synonym_phrase() -> None:
    family = families.find_family(["cordless", "drill"])
    assert family is not None
    assert family.core_term == "drill"


def test_find_family_returns_none_for_unregistered_category() -> None:
    assert families.find_family(["vintage", "lamp"]) is None


def test_register_family_is_configurable() -> None:
    families.register_family("saw", strong_synonyms=["saw", "circular saw"], related_synonyms=["jigsaw"])
    family = families.find_family(["saw"])
    assert family is not None
    assert "circular saw" in family.strong_synonyms
    assert "jigsaw" in family.related_synonyms


# =====================================================================
# accessories.py - accessory vocabulary
# =====================================================================


def test_default_accessory_terms_are_recognized() -> None:
    vocab = accessories.accessory_vocabulary()
    matches = find_phrase_matches(tokenize("Makita Battery Holder Wall Mount"), vocab)
    assert "battery holder" in matches
    assert "wall mount" in matches


def test_register_accessory_term_extends_vocabulary() -> None:
    accessories.register_accessory_term("carrying strap")
    matches = find_phrase_matches(tokenize("Drill Carrying Strap"), accessories.accessory_vocabulary())
    assert "carrying strap" in matches


# =====================================================================
# query.py - query parsing
# =====================================================================


def test_parse_query_extracts_recognized_brand_and_core_tokens() -> None:
    parsed = parse_query("Makita drill")
    assert parsed.brands == frozenset({"makita"})
    assert parsed.core_tokens == ["drill"]


def test_parse_query_handles_multi_word_brand_alias() -> None:
    parsed = parse_query("Black and Decker drill")
    assert parsed.brands == frozenset({"black+decker"})
    assert parsed.core_tokens == ["drill"]


def test_parse_query_with_no_recognized_brand_keeps_all_core_tokens() -> None:
    parsed = parse_query("vintage lamp")
    assert parsed.brands == frozenset()
    assert parsed.core_tokens == ["vintage", "lamp"]


def test_parse_query_brand_only_query_has_no_core_tokens() -> None:
    parsed = parse_query("Makita")
    assert parsed.brands == frozenset({"makita"})
    assert parsed.core_tokens == []


# =====================================================================
# evaluate_relevance - required scenarios: "Makita drill"
# =====================================================================


def test_makita_drill_accepts_makita_cordless_drill() -> None:
    result = evaluate_relevance("Makita drill", _listing("Makita 18V Cordless Drill Driver"))
    assert result.is_relevant is True
    assert result.rejected_reason is None


def test_makita_drill_accepts_makita_hammer_drill() -> None:
    result = evaluate_relevance("Makita drill", _listing("Makita Hammer Drill XPH12Z"))
    assert result.is_relevant is True


def test_makita_drill_rejects_makita_battery_holder() -> None:
    result = evaluate_relevance("Makita drill", _listing("Makita Battery Holder Wall Mount"))
    assert result.is_relevant is False
    assert result.rejected_reason == "accessory_without_core_product_match"


def test_makita_drill_rejects_dewalt_drill_as_brand_conflict() -> None:
    result = evaluate_relevance("Makita drill", _listing("DeWalt 20V Drill DCD771C2"))
    assert result.is_relevant is False
    assert result.rejected_reason == "brand_conflict"


def test_makita_drill_rejects_milwaukee_battery_holder() -> None:
    result = evaluate_relevance("Makita drill", _listing("Milwaukee Battery Holder Rack"))
    assert result.is_relevant is False
    # Two independent reasons this listing is wrong (wrong brand AND an
    # accessory) - brand conflict is checked first and wins.
    assert result.rejected_reason == "brand_conflict"


def test_makita_drill_accepts_generic_drill_with_no_brand_mentioned() -> None:
    """Policy: a brand-neutral listing (no recognized brand mentioned at
    all) is not penalized just because the query named a brand - plenty of
    real listings simply don't put the brand in the title."""
    result = evaluate_relevance("Makita drill", _listing("Cordless Drill Generic Brand"))
    assert result.is_relevant is True


def test_makita_drill_accepts_makita_impact_driver_as_related_not_strong() -> None:
    """Policy: an impact driver is a related-but-not-identical product to a
    drill - scored lower than an exact drill match, but still relevant
    (matches the task's "potentially relevant" characterization)."""
    strong = evaluate_relevance("Makita drill", _listing("Makita Hammer Drill XPH12Z"))
    related = evaluate_relevance("Makita drill", _listing("Makita Impact Driver XDT131"))
    assert related.is_relevant is True
    assert related.score < strong.score


# --- "Bosch drill" ------------------------------------------------------


def test_bosch_drill_accepts_bosch_rotary_hammer_drill() -> None:
    result = evaluate_relevance("Bosch drill", _listing("Bosch Rotary Hammer Drill GBH"))
    assert result.is_relevant is True


def test_bosch_drill_rejects_bosch_drill_bit_holder() -> None:
    result = evaluate_relevance("Bosch drill", _listing("Bosch Drill Bit Holder Organizer"))
    assert result.is_relevant is False
    assert result.rejected_reason == "accessory_without_core_product_match"


def test_bosch_drill_rejects_makita_drill_as_brand_conflict() -> None:
    result = evaluate_relevance("Bosch drill", _listing("Makita Cordless Drill 18V"))
    assert result.is_relevant is False
    assert result.rejected_reason == "brand_conflict"


# --- "Makita battery holder" --------------------------------------------


def test_makita_battery_holder_accepts_makita_battery_holder() -> None:
    """The one case an accessory term must NOT be penalized: the query
    itself explicitly asked for the accessory."""
    result = evaluate_relevance("Makita battery holder", _listing("Makita Battery Holder Wall Mount"))
    assert result.is_relevant is True


def test_makita_battery_holder_rejects_dewalt_battery_holder_as_brand_conflict() -> None:
    result = evaluate_relevance("Makita battery holder", _listing("DeWalt Battery Holder Rack"))
    assert result.is_relevant is False
    assert result.rejected_reason == "brand_conflict"


def test_makita_battery_holder_rejects_makita_drill() -> None:
    """Policy call: a listing for the main product does not automatically
    satisfy a search for an accessory of that product - "Makita drill"
    does not contain "battery holder", so it doesn't count as a core
    match for this query."""
    result = evaluate_relevance("Makita battery holder", _listing("Makita Cordless Drill 18V"))
    assert result.is_relevant is False
    assert result.rejected_reason == "no_core_product_match"


# --- bare brand-only query (no core product term at all) ----------------


def test_brand_only_query_accepts_a_listing_that_mentions_the_brand() -> None:
    result = evaluate_relevance("Makita", _listing("Makita Plunge Base DRT50 RT0700 Adaptor"))
    assert result.is_relevant is True


def test_brand_only_query_rejects_a_brand_neutral_listing() -> None:
    """Regression test for a real bug found via `core/persistence/cleanup.py`'s
    historical re-evaluation against production data: a bare brand-only
    query (no core product term left after removing the brand) was
    treated as relevant to ANY listing that didn't mention a *different*
    brand - including listings that don't mention the queried brand at
    all. "Doesn't conflict with a different brand" is not the same claim
    as "is actually about this brand" - there's no core term left to
    supply a positive signal the way there is for e.g. "Makita drill" +
    "Cordless Drill Generic Brand"."""
    result = evaluate_relevance("Makita", _listing("Pokemon Charizard Holo Card 1999"))
    assert result.is_relevant is False
    assert result.rejected_reason == "brand_only_query_not_mentioned"


def test_brand_only_query_still_rejects_a_conflicting_brand() -> None:
    result = evaluate_relevance("Makita", _listing("DeWalt 20V Drill DCD771C2"))
    assert result.is_relevant is False
    assert result.rejected_reason == "brand_conflict"


# --- bare "drill" (no brand) ---------------------------------------------


@pytest.mark.parametrize(
    "title",
    ["Bosch Rotary Hammer Drill", "Makita Cordless Drill 18V", "DeWalt 20V Drill DCD771C2"],
)
def test_bare_drill_search_accepts_any_brands_drill(title: str) -> None:
    result = evaluate_relevance("drill", _listing(title))
    assert result.is_relevant is True


def test_bare_drill_search_rejects_battery_holder() -> None:
    result = evaluate_relevance("drill", _listing("Generic Battery Holder Organizer"))
    assert result.is_relevant is False


# --- normalization robustness -------------------------------------------


def test_punctuation_case_and_whitespace_do_not_change_the_verdict() -> None:
    baseline = evaluate_relevance("Makita drill", _listing("Makita Cordless Drill 18V"))
    messy = evaluate_relevance("  MAKITA,,  drill!!  ", _listing("makita---cordless...drill 18V"))
    assert messy.is_relevant == baseline.is_relevant is True
    assert messy.score == baseline.score


# --- partial-overlap / lenient-fallback behavior (documented tradeoff) --


def test_makita_battery_holder_partial_brand_overlap_is_not_enough_for_a_drill_search() -> None:
    """A shared brand token alone must not be enough - "Makita drill" must
    reject a Makita listing whose only real content is an accessory term,
    even though "makita" itself matched."""
    result = evaluate_relevance("Makita drill", _listing("Makita Battery Holder Wall Mount"))
    assert result.is_relevant is False


def test_unregistered_product_category_falls_back_to_lenient_token_overlap() -> None:
    """Documented design tradeoff: for a query naming a product category
    with no registered family/accessory entry, ANY shared token is treated
    as a full match rather than a stricter proportional score. This keeps
    the filter from silently rejecting legitimate results in categories
    the vocabulary doesn't know about (this module only curates tool/
    accessory terms) - see `evaluator.py`'s `_score_core_match`."""
    result = evaluate_relevance("vintage lamp", _listing("Antique Vintage Table Lamp"))
    assert result.is_relevant is True


# =====================================================================
# Product-hardening audit regressions - real false positives/negatives
# found by testing the exact queries named in that task: Makita/Bosch/
# Milwaukee drill, Fender Stratocaster, Gibson Les Paul, DeWalt impact
# driver. See CHANGELOG.md's product-hardening-pass entry for the full
# write-up of what was found and why each fix works.
# =====================================================================


@pytest.mark.parametrize(
    "query,title",
    [
        # A genuine tool KIT that happens to mention included accessories
        # must NOT be rejected just because it also mentions them - found
        # as a real false positive (these used to score 40, below
        # threshold, purely because "case"/"battery"/"charger" appeared
        # anywhere in an otherwise-clearly-a-drill title).
        ("Makita drill", "Makita XFD131 18V Cordless Drill Driver Kit with Carrying Case"),
        ("Bosch drill", "Bosch 18V Rotary Hammer Drill with Case and 2 Batteries"),
        ("Milwaukee drill", "Milwaukee M18 FUEL Drill/Driver Kit w/ Battery and Charger"),
    ],
)
def test_multi_word_family_match_exempts_a_genuine_kit_from_the_accessory_penalty(query: str, title: str) -> None:
    result = evaluate_relevance(query, _listing(title))
    assert result.is_relevant is True


def test_single_word_family_match_still_gets_the_full_accessory_penalty() -> None:
    """The exemption above must be narrow: a *bare* single-word family
    match (just "drill", not a multi-word synonym like "cordless drill")
    is NOT enough - "Bosch Drill Bit Holder Organizer" must still be
    rejected exactly as before, since "drill" there only describes what
    kind of holder it is, not that a drill is for sale."""
    result = evaluate_relevance("Bosch drill", _listing("Bosch Drill Bit Holder Organizer"))
    assert result.is_relevant is False
    assert result.rejected_reason == "accessory_without_core_product_match"


@pytest.mark.parametrize(
    "query,title",
    [
        ("Makita drill", "Makita BL1850B 18V 5.0Ah LXT Lithium-Ion Battery"),
        ("Makita drill", "Makita DC18RC 18V Rapid Optimum Charger"),
        ("DeWalt impact driver", "DeWalt DCB205 20V MAX 5.0Ah Battery"),
        ("DeWalt impact driver", "DeWalt DCB115 Fast Charger"),
    ],
)
def test_standalone_battery_or_charger_listing_is_rejected(query: str, title: str) -> None:
    """A battery/charger sold on its own (no drill-family term at all) was
    already correctly rejected before this audit, via no_core_product_match
    - confirmed still true now that "battery"/"charger" are also
    registered accessory terms in their own right."""
    result = evaluate_relevance(query, _listing(title))
    assert result.is_relevant is False


@pytest.mark.parametrize(
    "title",
    [
        "Fender Stratocaster Pickguard White 3-Ply",
        "Fender Stratocaster Pickup Set Alnico V",
        "Fender Stratocaster Replacement Neck Maple",
        "Fender Stratocaster Body Alder Unfinished",
        "Fender Stratocaster Hard Case",
        "Fender Stratocaster Gig Bag",
        "Guitar Strap for Fender Stratocaster",
        "Fender Stratocaster Owner's Manual",
    ],
)
def test_fender_stratocaster_search_rejects_guitar_parts_and_accessories(title: str) -> None:
    result = evaluate_relevance("Fender Stratocaster", _listing(title))
    assert result.is_relevant is False
    assert result.rejected_reason == "accessory_without_core_product_match"


def test_fender_stratocaster_search_accepts_a_complete_guitar() -> None:
    result = evaluate_relevance("Fender Stratocaster", _listing("Fender American Professional II Stratocaster"))
    assert result.is_relevant is True


def test_fender_stratocaster_search_accepts_a_complete_guitar_whose_description_mentions_body_and_neck() -> None:
    """The exact production bug: a real, detailed guitar listing's
    description routinely mentions body/neck wood - this must not be
    treated the same as an accessory listing whose TITLE says 'Body' or
    'Neck'. Description-only mentions are not what's being sold."""
    result = evaluate_relevance(
        "Fender Stratocaster",
        _listing(
            "Fender American Professional II Stratocaster",
            description="Features an alder body and a maple neck with rosewood fingerboard, comes with hard case.",
        ),
    )
    assert result.is_relevant is True


@pytest.mark.parametrize(
    "title",
    [
        "FENDER FENDER Stratocaster Masterbuilt John English 1996 1996",
        "FENDER FENDER STRATOCASTER WALNUT de 1972 1972",
        "Fender Stratocaster ST-110FIM Iron Maiden Signature Stratocaster 2001 - 2002 - Black",
    ],
)
def test_fender_stratocaster_search_accepts_real_production_listing_titles(title: str) -> None:
    """Regression for the exact three listings found live on Reverb that
    were incorrectly rejected - see PROJECT_CONTEXT.md decision #24."""
    result = evaluate_relevance("Fender Stratocaster", _listing(title))
    assert result.is_relevant is True


def test_fender_stratocaster_search_still_rejects_a_title_level_accessory_even_with_description() -> None:
    """The fix must stay narrow: an accessory word IN THE TITLE (what's
    actually being sold) must still be rejected, regardless of what the
    description additionally says."""
    result = evaluate_relevance(
        "Fender Stratocaster",
        _listing("Fender Stratocaster Pickguard White 3-Ply", description="Fits American Standard Stratocaster bodies."),
    )
    assert result.is_relevant is False
    assert result.rejected_reason == "accessory_without_core_product_match"


def test_makita_drill_search_accepts_a_bare_drill_whose_description_mentions_included_battery() -> None:
    """Same fix, drill category: a bare 'drill' match (not the multi-word
    'cordless drill' exemption) with an incidental battery/charger mention
    only in the description must not be penalized the way a title-level
    accessory listing correctly still is."""
    result = evaluate_relevance(
        "Makita drill",
        _listing("Makita XFD131 18V Drill", description="Sold with battery and charger included, gently used."),
    )
    assert result.is_relevant is True


@pytest.mark.parametrize(
    "title",
    [
        "Fender Stratocaster with Billy Corgan Pickups",
        "Fender Fender Stratocaster w/Pau Ferro Neck MIM Stratocaster",
        "Fender Stratocaster Hardtail with 3-Bolt Neck, Rosewood Fretboard",
    ],
)
def test_fender_stratocaster_search_accepts_a_complete_guitar_with_included_feature_described(title: str) -> None:
    """Regression for real Reverb false rejections found after the
    title-only accessory fix: 'with'/'w/' immediately before an accessory
    term marks it as a described feature of the complete guitar, not the
    item being sold - see PROJECT_CONTEXT.md decision #24."""
    result = evaluate_relevance("Fender Stratocaster", _listing(title))
    assert result.is_relevant is True


def test_fender_stratocaster_search_still_rejects_a_bare_neck_with_no_inclusion_marker() -> None:
    """The rule must stay narrow: no 'with'/'w' before the accessory term
    means it's still read as the head noun - the item being sold."""
    result = evaluate_relevance("Fender Stratocaster", _listing("Fender Stratocaster 1973-1976 thin neck"))
    assert result.is_relevant is False
    assert result.rejected_reason == "accessory_without_core_product_match"


@pytest.mark.parametrize(
    "title",
    ["Replacement Neck for Fender Stratocaster", "Hard Case for Fender Stratocaster"],
)
def test_fender_stratocaster_search_rejects_for_preposition_accessories(title: str) -> None:
    """'for' is not 'with' - opposite meaning (accessory FOR the product,
    not product WITH the accessory) - must not be treated as an inclusion
    marker."""
    result = evaluate_relevance("Fender Stratocaster", _listing(title))
    assert result.is_relevant is False
    assert result.rejected_reason == "accessory_without_core_product_match"


def test_fender_stratocaster_search_still_rejects_when_the_sold_accessory_precedes_an_included_one() -> None:
    """An accessory that's genuinely the subject (no marker before it)
    still blocks relevance even if a LATER accessory in the same title
    is legitimately exempted."""
    result = evaluate_relevance("Fender Stratocaster", _listing("Fender Stratocaster Neck with case"))
    assert result.is_relevant is False


def test_makita_drill_search_accepts_a_drill_with_an_included_battery_in_the_title() -> None:
    result = evaluate_relevance("Makita drill", _listing("Makita drill with battery"))
    assert result.is_relevant is True


def test_makita_drill_search_rejects_a_battery_sold_for_a_drill() -> None:
    result = evaluate_relevance("Makita drill", _listing("battery for Makita drill"))
    assert result.is_relevant is False


@pytest.mark.parametrize("title", ["Gibson Les Paul Guitar Stand", "Gibson Les Paul Pickup Set"])
def test_gibson_les_paul_search_rejects_accessories(title: str) -> None:
    """Regression for a subtler false positive found while fixing the
    Fender case above: a naive "more than one query token overlaps ->
    unambiguous" heuristic would have wrongly exempted these too, since
    "Les Paul" (the model name) appears intact in both - but that's
    exactly as likely in an accessory listing for that model as in a
    listing for the model itself, unlike a curated multi-word family
    synonym. See `evaluator.py::_score_core_match`'s docstring."""
    result = evaluate_relevance("Gibson Les Paul", _listing(title))
    assert result.is_relevant is False
    assert result.rejected_reason == "accessory_without_core_product_match"


def test_gibson_les_paul_search_still_rejects_accessories_with_description_populated() -> None:
    """Confirms the accessory rejections above aren't accidentally
    weakened by narrowing the accessory-penalty check to the title only -
    same title-level accessory word, now with a description present too."""
    result = evaluate_relevance(
        "Gibson Les Paul", _listing("Gibson Les Paul Guitar Stand", description="Sturdy stand for your Les Paul body and neck.")
    )
    assert result.is_relevant is False
    assert result.rejected_reason == "accessory_without_core_product_match"


def test_gibson_les_paul_search_accepts_a_complete_guitar() -> None:
    result = evaluate_relevance("Gibson Les Paul", _listing("Gibson Les Paul Standard 60s"))
    assert result.is_relevant is True


def test_fender_stratocaster_search_rejects_gibson_les_paul_as_brand_conflict() -> None:
    result = evaluate_relevance("Fender Stratocaster", _listing("Gibson Les Paul Standard"))
    assert result.is_relevant is False
    assert result.rejected_reason == "brand_conflict"


def test_gibson_les_paul_search_rejects_fender_stratocaster_as_brand_conflict() -> None:
    result = evaluate_relevance("Gibson Les Paul", _listing("Fender Stratocaster American Professional"))
    assert result.is_relevant is False
    assert result.rejected_reason == "brand_conflict"


def test_milwaukee_drill_search_rejects_milwaukee_drill_bit_case() -> None:
    result = evaluate_relevance("Milwaukee drill", _listing("Milwaukee Drill Bit Case"))
    assert result.is_relevant is False


def test_dewalt_impact_driver_search_accepts_the_tool_itself() -> None:
    result = evaluate_relevance("DeWalt impact driver", _listing("DeWalt 20V Impact Driver DCF887"))
    assert result.is_relevant is True


# =====================================================================
# filter_relevant_listings - the shared service entrypoint
# =====================================================================


def test_filter_relevant_listings_separates_relevant_from_rejected() -> None:
    listings = [
        _listing("Makita Cordless Drill 18V", external_id="rel-1"),
        _listing("Makita Battery Holder Wall Mount", external_id="rej-1"),
        _listing("DeWalt 20V Drill DCD771C2", external_id="rej-2"),
    ]
    result = filter_relevant_listings(query="Makita drill", listings=listings, marketplace="mock")
    assert [listing.external_listing_id for listing in result.relevant_listings] == ["rel-1"]
    assert result.raw_count == 3
    assert result.relevant_count == 1
    assert result.rejected_count == 2


def test_filter_relevant_listings_logs_required_fields_without_leaking_full_listing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    listings = [_listing("Makita Battery Holder Wall Mount", external_id="rej-1")]
    with caplog.at_level(logging.INFO, logger="marketplace_alert.core.relevance.service"):
        filter_relevant_listings(query="Makita drill", listings=listings, marketplace="mock", saved_search_id=42)

    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    for expected in ("42", "Makita drill", "mock", "rej-1", "accessory_without_core_product_match"):
        assert expected in message


def test_filter_relevant_listings_does_not_log_for_accepted_listings(caplog: pytest.LogCaptureFixture) -> None:
    listings = [_listing("Makita Cordless Drill 18V", external_id="rel-1")]
    with caplog.at_level(logging.INFO, logger="marketplace_alert.core.relevance.service"):
        filter_relevant_listings(query="Makita drill", listings=listings, marketplace="mock")

    assert caplog.records == []


# =====================================================================
# Integration: scheduled scan / Run Now share the same relevance path
# =====================================================================


class FakeConnector:
    """Connector-shaped fake returning a fixed list of listings, regardless
    of query - same pattern as tests/test_saved_search_scheduler.py."""

    def __init__(self, listings: list[Listing]) -> None:
        self._listings = listings

    def search(self, query: str, filters: dict | None = None) -> list[Listing]:
        return self._listings


class RecordingProvider(NotificationProvider):
    def __init__(self) -> None:
        self.sent: list[Listing] = []

    @property
    def is_enabled(self) -> bool:
        return True

    def send_listing_alert(self, listing: Listing) -> None:
        self.sent.append(listing)


def _mixed_listings() -> list[Listing]:
    return [
        _listing("Makita Cordless Drill 18V", external_id="good-drill"),
        _listing("Makita Battery Holder Wall Mount", external_id="bad-holder"),
        _listing("DeWalt 20V Drill DCD771C2", external_id="bad-brand"),
    ]


def test_scheduled_scan_only_persists_and_notifies_relevant_listings(session_factory) -> None:
    setup_session = session_factory()
    SavedSearchRepository(setup_session).create(
        query="Makita drill", marketplaces=["good"], scan_interval_seconds=60, is_active=True
    )
    setup_session.commit()
    setup_session.close()

    provider = RecordingProvider()
    runner = SavedSearchRunner(
        notification_service=NotificationService(provider),
        resolve_connector=lambda name: FakeConnector(_mixed_listings()),
    )
    scanner = BackgroundScanner(session_factory=session_factory, runner=runner, run_guard=SavedSearchRunGuard())

    scanner.run_due_searches()

    assert len(provider.sent) == 1
    assert provider.sent[0].external_listing_id == "good-drill"

    verify_session = session_factory()
    persisted_ids = {row.external_listing_id for row in verify_session.query(DiscoveredListing).all()}
    assert persisted_ids == {"good-drill"}
    verify_session.close()


def test_run_now_counts_reflect_post_filter_results(session_factory) -> None:
    """Run Now (`runner.run_by_id`, used by both the legacy and mobile
    manual-run endpoints) and the scheduler both call the exact same
    `SavedSearchRunner.run` -> `_run_one_marketplace` method - there is
    only one relevance-filtering code path for both to share."""
    setup_session = session_factory()
    saved_search = SavedSearchRepository(setup_session).create(
        query="Makita drill", marketplaces=["good"], scan_interval_seconds=60, is_active=True
    )
    setup_session.commit()
    saved_search_id = saved_search.id
    setup_session.close()

    provider = RecordingProvider()
    runner = SavedSearchRunner(
        notification_service=NotificationService(provider),
        resolve_connector=lambda name: FakeConnector(_mixed_listings()),
    )

    run_session = session_factory()
    result = runner.run_by_id(run_session, saved_search_id)
    run_session.commit()
    run_session.close()

    assert result is not None
    marketplace_result = result.results[0]
    assert marketplace_result.raw_count == 3
    assert marketplace_result.new_count == 1
    assert marketplace_result.rejected_count == 2
    assert len(provider.sent) == 1


def test_rejected_listing_is_never_persisted(session_factory) -> None:
    setup_session = session_factory()
    saved_search = SavedSearchRepository(setup_session).create(
        query="Makita drill", marketplaces=["good"], scan_interval_seconds=60, is_active=True
    )
    setup_session.commit()
    saved_search_id = saved_search.id
    setup_session.close()

    runner = SavedSearchRunner(
        notification_service=NotificationService(RecordingProvider()),
        resolve_connector=lambda name: FakeConnector(_mixed_listings()),
    )
    run_session = session_factory()
    runner.run_by_id(run_session, saved_search_id)
    run_session.commit()
    run_session.close()

    verify_session = session_factory()
    for rejected_id in ("bad-holder", "bad-brand"):
        assert (
            verify_session.query(DiscoveredListing)
            .filter_by(marketplace="good", external_listing_id=rejected_id)
            .first()
            is None
        )
    verify_session.close()


def test_rejected_listing_is_never_notified(session_factory) -> None:
    setup_session = session_factory()
    saved_search = SavedSearchRepository(setup_session).create(
        query="Makita drill", marketplaces=["good"], scan_interval_seconds=60, is_active=True
    )
    setup_session.commit()
    saved_search_id = saved_search.id
    setup_session.close()

    provider = RecordingProvider()
    runner = SavedSearchRunner(
        notification_service=NotificationService(provider),
        resolve_connector=lambda name: FakeConnector(_mixed_listings()),
    )
    run_session = session_factory()
    runner.run_by_id(run_session, saved_search_id)
    run_session.commit()
    run_session.close()

    notified_ids = {listing.external_listing_id for listing in provider.sent}
    assert "bad-holder" not in notified_ids
    assert "bad-brand" not in notified_ids


def test_duplicate_detection_still_works_for_relevant_listings_after_filtering(session_factory) -> None:
    setup_session = session_factory()
    saved_search = SavedSearchRepository(setup_session).create(
        query="Makita drill", marketplaces=["good"], scan_interval_seconds=60, is_active=True
    )
    setup_session.commit()
    saved_search_id = saved_search.id
    setup_session.close()

    runner = SavedSearchRunner(
        notification_service=NotificationService(RecordingProvider()),
        resolve_connector=lambda name: FakeConnector(_mixed_listings()),
    )

    first_session = session_factory()
    first_result = runner.run_by_id(first_session, saved_search_id)
    first_session.commit()
    first_session.close()
    assert first_result.results[0].new_count == 1
    assert first_result.results[0].already_seen_count == 0

    # Same connector output again: the relevant listing is now a duplicate
    # (already seen); the two irrelevant listings are rejected again by
    # relevance filtering - never counted as "already seen" since they
    # were never persisted the first time either.
    second_session = session_factory()
    second_result = runner.run_by_id(second_session, saved_search_id)
    second_session.commit()
    second_session.close()

    assert second_result.results[0].new_count == 0
    assert second_result.results[0].already_seen_count == 1
    assert second_result.results[0].rejected_count == 2


def test_one_failing_marketplace_does_not_affect_relevance_filtering_in_another(session_factory) -> None:
    """Resilience across marketplaces (existing behavior) composes cleanly
    with relevance filtering - a broken marketplace still doesn't stop a
    healthy one's listings from being filtered, persisted, and notified."""

    class BrokenConnector:
        def search(self, query: str, filters: dict | None = None) -> list[Listing]:
            raise RuntimeError("simulated connector failure")

    setup_session = session_factory()
    SavedSearchRepository(setup_session).create(
        query="Makita drill", marketplaces=["good", "broken"], scan_interval_seconds=60, is_active=True
    )
    setup_session.commit()
    setup_session.close()

    provider = RecordingProvider()

    def resolve_connector(marketplace: str):
        return FakeConnector(_mixed_listings()) if marketplace == "good" else BrokenConnector()

    runner = SavedSearchRunner(notification_service=NotificationService(provider), resolve_connector=resolve_connector)
    scanner = BackgroundScanner(session_factory=session_factory, runner=runner, run_guard=SavedSearchRunGuard())

    scanner.run_due_searches()

    assert len(provider.sent) == 1
    assert provider.sent[0].external_listing_id == "good-drill"


# =====================================================================
# Integration: legacy /scan endpoint
# =====================================================================


def test_scan_endpoint_filters_irrelevant_listings_before_persistence_and_notification(
    client, fake_notification_provider, monkeypatch: pytest.MonkeyPatch
) -> None:
    import marketplace_alert.main as main_module

    monkeypatch.setattr(main_module, "_mock_connector", FakeConnector(_mixed_listings()))

    response = client.get("/scan", params={"q": "Makita drill"})
    assert response.status_code == 200
    body = response.json()

    assert body["new_count"] == 1
    assert [listing["external_listing_id"] for listing in body["new_listings"]] == ["good-drill"]
    assert body["raw_count"] == 3
    assert body["rejected_count"] == 2
    assert len(fake_notification_provider.sent_listings) == 1
    assert fake_notification_provider.sent_listings[0].external_listing_id == "good-drill"


def test_scan_endpoint_does_not_persist_rejected_listings(
    client, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    import marketplace_alert.main as main_module

    monkeypatch.setattr(main_module, "_mock_connector", FakeConnector(_mixed_listings()))

    client.get("/scan", params={"q": "Makita drill"})

    for rejected_id in ("bad-holder", "bad-brand"):
        assert (
            db_session.query(DiscoveredListing)
            .filter_by(marketplace="mock", external_listing_id=rejected_id)
            .first()
            is None
        )


# =====================================================================
# Integration: legacy /saved-searches/{id}/run and mobile
# /api/v1/saved-searches/{id}/run both filter through the same runner
# =====================================================================


def test_legacy_run_now_endpoint_filters_irrelevant_listings(
    client, fake_notification_provider, monkeypatch: pytest.MonkeyPatch
) -> None:
    import marketplace_alert.main as main_module

    monkeypatch.setattr(main_module, "get_connector", lambda name: FakeConnector(_mixed_listings()))

    created = client.post(
        "/saved-searches",
        json={"query": "Makita drill", "marketplaces": ["mock"], "scan_interval_seconds": 60, "is_active": True},
    ).json()

    response = client.post(f"/saved-searches/{created['id']}/run")
    assert response.status_code == 200
    body = response.json()

    result = body["results"][0]
    assert result["new_count"] == 1
    assert result["raw_count"] == 3
    assert result["rejected_count"] == 2
    assert len(fake_notification_provider.sent_listings) == 1
    assert fake_notification_provider.sent_listings[0].external_listing_id == "good-drill"


def test_mobile_run_now_endpoint_filters_irrelevant_listings(
    client, fake_notification_provider, monkeypatch: pytest.MonkeyPatch
) -> None:
    import marketplace_alert.api.v1.saved_searches as saved_searches_module

    monkeypatch.setattr(saved_searches_module, "get_connector", lambda name: FakeConnector(_mixed_listings()))

    created = client.post(
        "/api/v1/saved-searches",
        json={"query": "Makita drill", "marketplaces": ["mock"], "scan_interval_seconds": 60, "is_active": True},
    ).json()

    response = client.post(f"/api/v1/saved-searches/{created['id']}/run")
    assert response.status_code == 200
    body = response.json()

    outcome = body["marketplaces"]["mock"]
    assert outcome["new_count"] == 1
    assert outcome["raw_count"] == 3
    assert outcome["rejected_count"] == 2
    assert body["total_new_count"] == 1
    assert len(fake_notification_provider.sent_listings) == 1
    assert fake_notification_provider.sent_listings[0].external_listing_id == "good-drill"
