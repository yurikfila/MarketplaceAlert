"""Duplicate / overlap detection for saved searches (advisory only).

Nothing here creates, changes, merges or deletes a saved search. It only
*describes* how a proposed search relates to the same user's existing ones,
so a client can warn ("you already track this") or offer to edit the
existing search instead. Creation is never blocked: intentionally broad or
intentionally overlapping searches stay possible.

Two searches are compared on a normalized fingerprint (Unicode-normalized,
stemmed, order-insensitive, stop words dropped, brand aliases resolved - so
"Bosch drill", "drill BOSCH" and "Drills, Bosch" compare equal) plus their
marketplace sets. Different users' searches are never compared (callers
pass only the owner's searches).

Notification duplicates are already prevented one layer down: the
notification outbox is idempotent per (user, listing), so two overlapping
searches of one user never produce two alerts for the same listing. These
warnings are about wasted scans and clutter, not double alerts.
"""

from dataclasses import dataclass

from marketplace_alert.core.relevance.engine_v2 import parse_query_v2

RELATION_IDENTICAL = "identical"
RELATION_OVERLAPPING_MARKETPLACES = "same_query_overlapping_marketplaces"
RELATION_COVERED_BY_BROADER = "covered_by_broader_search"
RELATION_COVERS_NARROWER = "covers_narrower_search"

WARNING_BRAND_ONLY = "brand_only_query"
WARNING_NO_SEARCHABLE_TERMS = "no_searchable_terms"


@dataclass(frozen=True)
class QueryFingerprint:
    keys: frozenset[str]
    is_brand_only: bool
    is_empty: bool


def fingerprint(query: str | None) -> QueryFingerprint:
    parsed = parse_query_v2(query)
    keys = frozenset({term.stem for term in parsed.terms} | {f"brand:{brand}" for brand in parsed.brands})
    return QueryFingerprint(keys=keys, is_brand_only=parsed.is_brand_only, is_empty=parsed.is_empty)


def relationship(
    new_query: QueryFingerprint,
    new_marketplaces: frozenset[str],
    existing_query: QueryFingerprint,
    existing_marketplaces: frozenset[str],
) -> str | None:
    """How an existing search relates to a proposed one, or `None` when it
    does not overlap (different terms, or no marketplace in common)."""
    if new_query.is_empty or existing_query.is_empty:
        return None
    if not (new_marketplaces & existing_marketplaces):
        return None
    if new_query.keys == existing_query.keys:
        return RELATION_IDENTICAL if new_marketplaces == existing_marketplaces else RELATION_OVERLAPPING_MARKETPLACES
    if existing_query.keys < new_query.keys:
        return RELATION_COVERED_BY_BROADER
    if new_query.keys < existing_query.keys:
        return RELATION_COVERS_NARROWER
    return None


def query_warnings(query: str | None) -> list[str]:
    fp = fingerprint(query)
    if fp.is_empty:
        return [WARNING_NO_SEARCHABLE_TERMS]
    if fp.is_brand_only:
        return [WARNING_BRAND_ONLY]
    return []
