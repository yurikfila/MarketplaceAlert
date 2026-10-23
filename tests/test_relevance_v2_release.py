"""MA-REL-02: release-readiness checks for relevance v2.

1. The expanded comparison corpus (`tests/relevance_corpus.py`), pinned for both engines.
2. The "Red Jacket" decision, pinned in both directions.
3. Shadow mode: legacy stays the only decision maker, v2 cannot interrupt a scan,
   nothing extra is persisted or enqueued, logs carry no user text, and log volume
   is bounded and countable.
4. Feature-flag behaviour end to end through the real scan path
   (`legacy` = today, `shadow` = legacy decisions, `v2` = new decisions).

Nothing here touches a real marketplace, Telegram, Expo or any production resource;
every scan uses a throw-away SQLite database and a fake connector.
"""

import logging

import pytest
from sqlalchemy.orm import sessionmaker

from marketplace_alert.config import settings
from marketplace_alert.core.models.listing import Listing
from marketplace_alert.core.persistence.database import Base, create_db_engine
from marketplace_alert.core.persistence.models import DiscoveredListing, PendingNotification
from marketplace_alert.core.relevance import accessories, brands, evaluate_relevance, evaluate_relevance_v2, families
from marketplace_alert.core.relevance import service as relevance_service
from marketplace_alert.core.relevance.service import filter_relevant_listings
from marketplace_alert.core.saved_searches.repository import SavedSearchRepository
from marketplace_alert.core.saved_searches.runner import SavedSearchRunner
from marketplace_alert.core.scheduler.guard import SavedSearchRunGuard
from marketplace_alert.core.scheduler.scanner import BackgroundScanner
from tests.relevance_corpus import CORPUS


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


def _l(title: str, external_id: str = "x", description: str | None = None) -> Listing:
    return Listing(
        marketplace="m",
        external_listing_id=external_id,
        title=title,
        description=description,
        listing_url=f"https://example.com/{external_id}",
    )


# =====================================================================
# 1. Expanded corpus
# =====================================================================

DOMAINS = {row[0] for row in CORPUS}


def test_corpus_covers_every_requested_domain() -> None:
    assert DOMAINS >= {
        "electronics", "tools", "clothing", "music", "collectibles", "automotive",
        "hebrew", "russian", "brand-only", "model-id",
    }  # fmt: skip
    assert len(CORPUS) >= 150


@pytest.mark.parametrize(
    ("domain", "query", "title", "wanted", "legacy", "v2"),
    CORPUS,
    ids=[f"{r[0]}:{r[1]}|{r[2][:40]}" for r in CORPUS],
)
def test_corpus_row_decisions_are_pinned_for_both_engines(domain, query, title, wanted, legacy, v2) -> None:
    assert evaluate_relevance(query, _l(title)).is_relevant is legacy, "legacy decision changed"
    assert evaluate_relevance_v2(query, _l(title)).is_relevant is v2, "v2 decision changed"


def test_corpus_summary_is_what_the_release_report_states() -> None:
    total = len(CORPUS)
    disagree = [r for r in CORPUS if r[4] != r[5]]
    stricter = [r for r in disagree if r[4] and not r[5]]
    looser = [r for r in disagree if not r[4] and r[5]]
    legacy_errors = sum(1 for r in CORPUS if r[4] != r[3])
    v2_errors = sum(1 for r in CORPUS if r[5] != r[3])
    assert (total, len(disagree), len(stricter), len(looser)) == (158, 37, 28, 9)
    assert (legacy_errors, v2_errors) == (41, 4)


def test_every_v2_error_on_the_corpus_is_a_known_documented_limitation() -> None:
    """If a new row is wrong for v2 it must be added here on purpose, with its reason."""
    known = {
        # body/neck are accessory words (inherited from the legacy vocabulary): complete products are rejected
        ("canon eos r6", "Canon EOS R6 Mirrorless Camera Body Only"),
        ("fender stratocaster", "Fender Player Stratocaster Pau Ferro Neck Sunburst"),
        # model numbers must match exactly: a kit suffix (C2) is a different identifier
        ("dewalt dcd771", "DeWalt DCD771C2 20V MAX Cordless Drill Kit"),
        # the engine is lexical: a controller is not in the accessory vocabulary
        ("playstation 5", "PlayStation 5 DualSense Controller White"),
    }
    wrong = {(r[1], r[2]) for r in CORPUS if r[5] != r[3]}
    assert wrong == known


def test_disagreements_are_explainable_by_a_reason_code() -> None:
    """Every row where the engines differ must carry a machine-readable reason on the rejecting side."""
    reasons: dict[str, int] = {}
    for _domain, query, title, _wanted, legacy, v2 in CORPUS:
        if legacy == v2:
            continue
        rejecting = evaluate_relevance_v2(query, _l(title)) if not v2 else evaluate_relevance(query, _l(title))
        assert rejecting.rejected_reason, (query, title)
        reasons[rejecting.rejected_reason] = reasons.get(rejecting.rejected_reason, 0) + 1
    assert sum(reasons.values()) == 37
    assert reasons["missing_required_terms"] >= 20  # the dominant, intended difference


def test_v2_never_rejects_a_wanted_listing_that_legacy_accepted_except_documented_cases() -> None:
    regressions = [(r[1], r[2]) for r in CORPUS if r[3] and r[4] and not r[5]]
    assert regressions == []


def test_brand_only_searches_stay_broad_in_both_engines() -> None:
    rows = [r for r in CORPUS if r[0] == "brand-only" and r[3]]
    assert rows
    for _domain, query, title, _wanted, legacy, v2 in rows:
        assert legacy and v2, (query, title)


# =====================================================================
# 2. The "Red Jacket" decision
# =====================================================================


def test_red_jacket_query_accepts_the_more_specific_listing() -> None:
    """The reverse of the reported case: a short query finds a longer title."""
    assert evaluate_relevance_v2("Red Jacket", _l("Red Leather Jacket")).is_relevant
    assert evaluate_relevance_v2("Red Jacket", _l("Red Leather Jacket Mens Large")).is_relevant


def test_a_more_specific_query_rejects_a_listing_missing_a_requested_word_by_design() -> None:
    """The actual regression: query "red leather jacket" vs. title "Red Jacket".

    Intentional: the user asked for leather and the title does not say it. Legacy accepted it
    only through a lenient "any two shared words" fallback, which equally accepts "Blue Leather
    Jacket" (wrong colour) and "Leather Jacket". The description cannot rescue it either - the
    title-only principle exists because a description mentioning a word in passing caused real
    production false positives. If shadow data shows this trade-off costs real alerts, the
    narrow remedy is description-based credit for queries of 3+ words, behind its own tests.
    """
    result = evaluate_relevance_v2("red leather jacket", _l("Red Jacket", description="Genuine leather, size M"))
    assert not result.is_relevant
    assert result.rejected_reason == "missing_required_terms"
    assert result.missing_terms == ("leather",)
    assert evaluate_relevance("red leather jacket", _l("Red Jacket")).is_relevant  # legacy: lenient
    assert evaluate_relevance("red leather jacket", _l("Blue Leather Jacket")).is_relevant  # ...and wrong
    assert not evaluate_relevance_v2("red leather jacket", _l("Blue Leather Jacket")).is_relevant


# =====================================================================
# 3 & 4. Shadow mode and the feature flag, end to end
# =====================================================================


class FakeConnector:
    def __init__(self, listings: list[Listing]) -> None:
        self._listings = listings

    def search(self, query: str, filters: dict | None = None) -> list[Listing]:
        return self._listings


SCAN_LISTINGS = [
    _l("Bosch Cordless Drill 18V", "drill", description="private-description-xyz"),
    _l("Bosch Diesel Filter", "filter"),
    _l("Bosch Dishwasher Spare Part", "dishwasher"),
    _l("Bosch Drill Bit Set 10pc", "bits"),  # legacy accepts, v2 rejects
    _l("Bosch Drilling Machine GBM 10", "machine"),  # legacy rejects, v2 accepts
]


def _scan(tmp_path, *, query: str, listings: list[Listing]) -> tuple[set[str], list[str], int]:
    """One real scan through SavedSearchRunner/BackgroundScanner on a throw-away DB.
    Returns (persisted listing ids, outbox listing ids in order, outbox row count)."""
    engine = create_db_engine(f"sqlite:///{tmp_path / 'scan.db'}")
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    setup = session_factory()
    SavedSearchRepository(setup).create(query=query, marketplaces=["m"], scan_interval_seconds=60, is_active=True)
    setup.commit()
    setup.close()
    runner = SavedSearchRunner(resolve_connector=lambda name: FakeConnector(listings))
    BackgroundScanner(session_factory=session_factory, runner=runner, run_guard=SavedSearchRunGuard()).run_due_searches()
    verify = session_factory()
    try:
        persisted = {row.external_listing_id for row in verify.query(DiscoveredListing).all()}
        outbox = [
            listing_id
            for (listing_id,) in verify.query(DiscoveredListing.external_listing_id)
            .join(PendingNotification, PendingNotification.discovered_listing_id == DiscoveredListing.id)
            .order_by(PendingNotification.id)
            .all()
        ]
        count = verify.query(PendingNotification).count()
    finally:
        verify.close()
        engine.dispose()
    return persisted, outbox, count


@pytest.fixture()
def engine_setting(monkeypatch):
    def _set(value: str) -> None:
        monkeypatch.setattr(settings, "relevance_engine", value)

    return _set


def test_flag_legacy_is_todays_behaviour(tmp_path, engine_setting) -> None:
    engine_setting("legacy")
    persisted, outbox, count = _scan(tmp_path, query="Bosch drill", listings=SCAN_LISTINGS)
    assert persisted == {"drill", "bits"}
    assert sorted(outbox) == ["bits", "drill"] and count == 2


def test_flag_shadow_makes_exactly_the_legacy_decisions(tmp_path, engine_setting) -> None:
    (tmp_path / "legacy").mkdir()
    (tmp_path / "shadow").mkdir()
    engine_setting("legacy")
    legacy = _scan(tmp_path / "legacy", query="Bosch drill", listings=SCAN_LISTINGS)
    engine_setting("shadow")
    shadow = _scan(tmp_path / "shadow", query="Bosch drill", listings=SCAN_LISTINGS)
    assert shadow == legacy  # same persisted listings, same outbox rows in the same order, same count


def test_flag_v2_makes_the_new_decisions(tmp_path, engine_setting) -> None:
    engine_setting("v2")
    persisted, outbox, count = _scan(tmp_path, query="Bosch drill", listings=SCAN_LISTINGS)
    assert persisted == {"drill", "machine"}
    assert sorted(outbox) == ["drill", "machine"] and count == 2


def test_shadow_adds_no_extra_persistence_or_notifications(tmp_path, engine_setting) -> None:
    engine_setting("shadow")
    persisted, _outbox, count = _scan(tmp_path, query="Bosch drill", listings=SCAN_LISTINGS)
    assert "machine" not in persisted  # v2-only acceptance was never persisted
    assert count == len(persisted) == 2


def test_v2_failure_in_shadow_mode_cannot_interrupt_scanning(tmp_path, engine_setting, monkeypatch, caplog) -> None:
    engine_setting("shadow")

    def boom(*args, **kwargs):
        raise RuntimeError("v2 exploded with secret-user-text")

    monkeypatch.setattr(relevance_service, "evaluate_relevance_v2", boom)
    with caplog.at_level(logging.INFO):
        persisted, outbox, count = _scan(tmp_path, query="Bosch drill", listings=SCAN_LISTINGS)
    assert persisted == {"drill", "bits"}  # exactly legacy's result
    assert sorted(outbox) == ["bits", "drill"] and count == 2
    assert "secret-user-text" not in caplog.text  # the exception message is never logged
    summaries = [r.getMessage() for r in caplog.records if "Relevance shadow summary" in r.getMessage()]
    assert len(summaries) == 1 and "v2_errors=5" in summaries[0] and "compared=0" in summaries[0]


def test_shadow_logs_never_contain_the_query_titles_or_descriptions(tmp_path, engine_setting, caplog) -> None:
    engine_setting("shadow")
    query = "Zorblax Frobnicator"
    listings = [
        _l("Zorblax Frobnicator Deluxe Edition", "a", description="private-description-xyz"),
        _l("Zorblax Gadget Frobnicator Charger", "b", description="private-description-xyz"),
        _l("Unrelated Teapot", "c"),
    ]
    with caplog.at_level(logging.DEBUG):
        _scan(tmp_path, query=query, listings=listings)
    text = caplog.text.lower()
    for secret in ("zorblax", "frobnicator", "deluxe edition", "private-description-xyz", "teapot"):
        assert secret not in text, secret


def test_shadow_log_volume_is_bounded_and_countable(monkeypatch, caplog) -> None:
    monkeypatch.setattr(settings, "relevance_shadow_max_detail_logs", 3)
    many = [_l(f"Bosch Drill Bit Set {i}", f"bits-{i}") for i in range(200)]  # legacy accepts all, v2 rejects all
    with caplog.at_level(logging.INFO, logger="marketplace_alert.core.relevance.service"):
        result = filter_relevant_listings(
            query="Bosch drill", listings=many, marketplace="m", saved_search_id=9, engine="shadow"
        )
    assert result.relevant_count == 200  # legacy decides
    shadow_lines = [r.getMessage() for r in caplog.records if "Relevance shadow" in r.getMessage()]
    details = [m for m in shadow_lines if "detail" in m]
    summaries = [m for m in shadow_lines if "summary" in m]
    assert len(details) == 3  # capped, not 200
    assert len(summaries) == 1  # exactly one summary line per filter call
    assert "compared=200" in summaries[0] and "disagreements=200" in summaries[0] and "v2_stricter=200" in summaries[0]
    assert "saved search 9" in summaries[0] and "marketplace m" in summaries[0]


def test_shadow_detail_lines_can_be_switched_off(monkeypatch, caplog) -> None:
    monkeypatch.setattr(settings, "relevance_shadow_max_detail_logs", 0)
    with caplog.at_level(logging.INFO, logger="marketplace_alert.core.relevance.service"):
        filter_relevant_listings(
            query="Bosch drill", listings=[_l("Bosch Drill Bit Set")], marketplace="m", engine="shadow"
        )
    messages = [r.getMessage() for r in caplog.records if "Relevance shadow" in r.getMessage()]
    assert len(messages) == 1 and "summary" in messages[0] and "disagreements=1" in messages[0]


def test_summary_counts_both_directions(caplog) -> None:
    listings = [
        _l("Bosch Drill Bit Set", "stricter"),  # legacy accept, v2 reject
        _l("Bosch Drilling Machine GBM", "looser"),  # legacy reject, v2 accept
        _l("Bosch Cordless Drill", "same"),
    ]
    with caplog.at_level(logging.INFO, logger="marketplace_alert.core.relevance.service"):
        filter_relevant_listings(query="Bosch drill", listings=listings, marketplace="m", engine="shadow")
    summary = next(r.getMessage() for r in caplog.records if "summary" in r.getMessage())
    assert "compared=3 disagreements=2 v2_stricter=1 v2_looser=1 v2_errors=0" in summary


def test_legacy_and_v2_modes_emit_no_shadow_lines(caplog) -> None:
    with caplog.at_level(logging.INFO, logger="marketplace_alert.core.relevance.service"):
        for engine in ("legacy", "v2"):
            filter_relevant_listings(query="Bosch drill", listings=[_l("Bosch Cordless Drill")], marketplace="m", engine=engine)
    assert not [r for r in caplog.records if "shadow" in r.getMessage().lower()]


def test_shadow_evaluates_v2_only_for_comparison_the_returned_listings_are_legacys() -> None:
    listings = [_l("Bosch Drill Bit Set", "bits"), _l("Bosch Drilling Machine GBM", "machine")]
    shadow = filter_relevant_listings(query="Bosch drill", listings=listings, marketplace="m", engine="shadow")
    legacy = filter_relevant_listings(query="Bosch drill", listings=listings, marketplace="m", engine="legacy")
    assert [x.external_listing_id for x in shadow.relevant_listings] == [x.external_listing_id for x in legacy.relevant_listings]
    assert (shadow.raw_count, shadow.relevant_count, shadow.rejected_count) == (
        legacy.raw_count,
        legacy.relevant_count,
        legacy.rejected_count,
    )
