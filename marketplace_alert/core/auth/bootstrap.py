"""Bootstrap-user cutover: the one-time (and safely re-runnable) process
that gives every pre-existing, pre-authentication `saved_searches` row an
owner, so user-scoped queries (`core/saved_searches/repository.py`'s
`*_owned` methods, `core/persistence/repository.py`'s `list_recent_owned`)
have something correct to return once routes are protected in a later
phase.

Pure logic only - no `argparse`, no `getpass`, no `print`. The CLI wrapper
(`scripts/create_bootstrap_admin.py`) owns all of that; everything here is
plain functions/dataclasses a test can call directly against a session,
matching this codebase's established split (see `core/persistence/
cleanup.py`/`backfill.py` and their respective `scripts/*.py` wrappers).

**Why this doesn't just call `AuthService.signup()`.** `AuthService.signup`
issues a token pair and commits internally (see `core/auth/service.py`) -
neither is wanted here: a script-created admin account has no legitimate
use for an immediately-orphaned refresh token nobody will ever present,
and an early internal commit would defeat the atomicity this module needs
(the saved-search/listing backfill must succeed or fail *together*, not
depend on whether user-creation happened to commit first on a previous,
partially-failed run). Instead, this module uses the same underlying
primitives `AuthService.signup` itself is built on -
`core.auth.security.hash_password` and `core.auth.repository.
UserRepository.create` - never a hand-rolled hash.

**Idempotency.** Every function here re-reads current state rather than
trusting anything computed on a previous call - `run_cutover` can be
called again (the CLI wrapper's normal "safe rerun" story) and will
simply find nothing left to do wherever a previous run already finished,
and pick up any saved searches/listings created since.
"""

from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from marketplace_alert.core.auth.models import User
from marketplace_alert.core.auth.repository import UserRepository
from marketplace_alert.core.auth.security import hash_password
from marketplace_alert.core.persistence.models import DiscoveredListing
from marketplace_alert.core.saved_searches.models import SavedSearch

MIN_BOOTSTRAP_PASSWORD_LENGTH = 8


class BootstrapPasswordTooShortError(ValueError):
    """Raised when a password shorter than `MIN_BOOTSTRAP_PASSWORD_LENGTH`
    is supplied for a *new* bootstrap account. Never raised on a rerun
    that finds an existing account - no password is needed then at all."""


@dataclass
class SavedSearchBackfillResult:
    """How many `saved_searches` rows had `user_id IS NULL` before this
    call, and (only in apply mode) how many were actually assigned."""

    unowned_before: int
    assigned_count: int


@dataclass
class ListingAttribution:
    """One listing this run would attribute (or did attribute) to one
    specific saved search - only ever created when
    `_find_provenance_backed_saved_search_id` finds genuine evidence, see
    that function's docstring."""

    listing_id: int
    saved_search_id: int


@dataclass
class ListingAttributionResult:
    """A full accounting of every `discovered_listings` row - never a
    guess, every count derived from the same pass. See
    `compute_listing_attribution`'s docstring for exactly what
    distinguishes each bucket, and in particular why
    `safely_attributable_count` requires genuine provenance evidence, not
    mere marketplace co-occurrence.

    - `already_attributed_count`: rows with `discovered_by_saved_search_id`
      already set, before this run - informational only, never touched by
      this module at all.
    - `safely_attributable_count`: rows this run attributes (or, in dry
      run, would attribute) - only ever when genuine provenance evidence
      exists. See `_find_provenance_backed_saved_search_id`.
    - `ambiguous_count`: **diagnostic only, never attributed** - rows
      where more than one saved search happens to target the same
      marketplace. Reported so a human reviewing the dry-run output can
      see *why* a row wasn't attributed, not because this count ever
      feeds an attribution decision.
    - `no_provenance_count` (a.k.a. "left unowned"): every other
      unattributed row - zero or exactly one marketplace-matching saved
      search, but with no genuine evidence tying the listing to it
      specifically. This is the expected, common case given the current
      schema - see this module's docstring.
    """

    total_unattributed: int
    already_attributed_count: int
    safely_attributable_count: int
    ambiguous_count: int
    no_provenance_count: int
    attributions: list[ListingAttribution] = field(default_factory=list)


@dataclass
class BootstrapReport:
    """The complete result of one `run_cutover` call - everything the CLI
    wrapper needs to print a report, in both dry-run and apply modes."""

    email: str
    user_id: int | None
    user_already_existed: bool
    saved_search_backfill: SavedSearchBackfillResult
    listing_attribution: ListingAttributionResult
    applied: bool


def find_bootstrap_user(session: Session, email: str) -> User | None:
    """Read-only - never creates anything. Used both to decide whether a
    password is even needed, and by dry-run mode, which must never touch
    the database at all."""
    return UserRepository(session).get_by_email(email)


def create_bootstrap_user(session: Session, *, email: str, password: str) -> User:
    """Create the bootstrap account. Flushes, does not commit - the
    caller (`run_cutover`, or a test) decides the transaction boundary,
    same convention as every repository method in this codebase.

    Raises `BootstrapPasswordTooShortError` before ever touching the
    database if `password` is shorter than
    `MIN_BOOTSTRAP_PASSWORD_LENGTH` - the same baseline this project's
    `POST /api/v1/auth/signup` enforces (`SignupRequest.password`'s
    `min_length`); an admin account deserves at least that bar too.
    """
    if len(password) < MIN_BOOTSTRAP_PASSWORD_LENGTH:
        raise BootstrapPasswordTooShortError(
            f"Password must be at least {MIN_BOOTSTRAP_PASSWORD_LENGTH} characters"
        )
    password_hash = hash_password(password)
    return UserRepository(session).create(email=email, password_hash=password_hash)


def list_unowned_saved_searches(session: Session) -> list[SavedSearch]:
    """Every saved search with `user_id IS NULL` right now - re-queried
    fresh on every call, never cached, so a rerun naturally picks up any
    saved search created since the last run."""
    stmt = select(SavedSearch).where(SavedSearch.user_id.is_(None)).order_by(SavedSearch.id)
    return list(session.execute(stmt).scalars().all())


def assign_saved_searches_to_user(session: Session, saved_searches: list[SavedSearch], user_id: int) -> int:
    """Assigns every given (already `user_id IS NULL`) saved search to
    `user_id`. Never called with anything but the output of
    `list_unowned_saved_searches` - there is no filter here re-checking
    `user_id IS NULL` because the caller's query already guarantees it;
    an already-owned saved search is never passed in, so it is never at
    risk of being reassigned. Flushes, does not commit."""
    for saved_search in saved_searches:
        saved_search.user_id = user_id
    session.flush()
    return len(saved_searches)


def _find_provenance_backed_saved_search_id(
    row: DiscoveredListing, candidate_ids_by_marketplace: dict[str, list[int]]
) -> int | None:
    """Returns a `saved_search_id` **only** when the stored data ties
    `row` to exactly one saved search via genuine provenance - never
    merely because a marketplace happens to have one (or even, after
    narrowing, exactly one) candidate saved search.

    **What was actually inspected before concluding this always returns
    `None` today**: every column on `DiscoveredListing` (`marketplace`,
    `external_listing_id`, `title`, `listing_url`, `price`, `currency`,
    `location`, `seller`, `condition`, `image_url`, `source_created_at`,
    `first_discovered_at`, `last_seen_at`, `metadata_backfill_status`)
    and on `SavedSearch` (`query`, `created_at`, `updated_at`,
    `last_scanned_at`, `marketplace_links`). None of them record which
    saved search actually discovered a given listing - that fact is only
    ever captured by `discovered_by_saved_search_id` itself, which is
    exactly the column missing for the rows this function is asked
    about. There is no stored query text on the listing, no relevance
    score, no cross-reference of any kind connecting a specific listing
    to a specific search.

    **Marketplace-uniqueness was considered and rejected, explicitly**
    (this was the prior, now-corrected version of this module's logic):
    "exactly one saved search currently targets this marketplace" is a
    *correlation*, not provenance - it says nothing about which search
    (if any still existing) actually found this specific listing. A
    listing could easily have been found by a different, since-deleted
    saved search on the same marketplace, and the current lone survivor
    is coincidental. Narrowing further by requiring
    `SavedSearch.created_at <= DiscoveredListing.first_discovered_at`
    (a saved search created after a listing was found provably could not
    have found it) was also considered - it's a valid *exclusion* rule,
    but composing it with marketplace-matching still produces a
    correlation-based guess, never a recorded fact, so it was rejected
    for the same reason. Fuzzy title/query text matching was not
    attempted at all - nothing about it could be made deterministic
    enough to trust for an irreversible-in-practice ownership decision.

    This function exists (as its own named place, rather than inlining
    "there is no evidence" at every call site) so that if a future schema
    change ever adds real provenance (e.g., a persisted "matched query"
    column), there is exactly one place to implement it, without
    disturbing the counting/reporting logic in
    `compute_listing_attribution` around it. `candidate_ids_by_marketplace`
    is accepted as a parameter now specifically so that future
    implementation has it ready to use, even though today's
    implementation ignores it entirely.
    """
    return None


def compute_listing_attribution(session: Session, *, candidate_saved_search_ids: list[int]) -> ListingAttributionResult:
    """Read-only accounting of every `discovered_listings` row - see
    `ListingAttributionResult`'s docstring for what each bucket means,
    and `_find_provenance_backed_saved_search_id`'s docstring for exactly
    what would (and currently does not) qualify a row for automatic
    attribution.

    `candidate_saved_search_ids` is deliberately provided by the caller,
    not computed here - `run_cutover` passes "every saved search that is
    or is about to become bootstrap-owned" (unowned-right-now, unioned
    with any already bootstrap-owned from a previous run). It is used
    only to build the `ambiguous`-vs-`no_provenance` diagnostic split
    below (both outcomes are identical - left unowned - so this
    distinction is informational, never decision-making).

    Never fabricates ownership to maximize how many rows end up
    attributed - a row this function can't produce genuine evidence for
    stays exactly as unowned as it already was.
    """
    already_attributed_count = session.execute(
        select(func.count())
        .select_from(DiscoveredListing)
        .where(DiscoveredListing.discovered_by_saved_search_id.is_not(None))
    ).scalar_one()

    candidates = (
        list(session.execute(select(SavedSearch).where(SavedSearch.id.in_(candidate_saved_search_ids))).scalars().all())
        if candidate_saved_search_ids
        else []
    )
    candidate_ids_by_marketplace: dict[str, list[int]] = {}
    for saved_search in candidates:
        for marketplace in saved_search.marketplaces:
            candidate_ids_by_marketplace.setdefault(marketplace, []).append(saved_search.id)

    unattributed_rows = list(
        session.execute(
            select(DiscoveredListing).where(DiscoveredListing.discovered_by_saved_search_id.is_(None)).order_by(
                DiscoveredListing.id
            )
        )
        .scalars()
        .all()
    )

    safely_attributable_count = 0
    ambiguous_count = 0
    no_provenance_count = 0
    attributions: list[ListingAttribution] = []

    for row in unattributed_rows:
        evidence = _find_provenance_backed_saved_search_id(row, candidate_ids_by_marketplace)
        if evidence is not None:
            safely_attributable_count += 1
            attributions.append(ListingAttribution(listing_id=row.id, saved_search_id=evidence))
            continue

        # No genuine provenance either way - the split below is purely
        # diagnostic (why not?), never used to attribute anything.
        marketplace_candidates = candidate_ids_by_marketplace.get(row.marketplace, [])
        if len(marketplace_candidates) >= 2:
            ambiguous_count += 1
        else:
            no_provenance_count += 1

    return ListingAttributionResult(
        total_unattributed=len(unattributed_rows),
        already_attributed_count=already_attributed_count,
        safely_attributable_count=safely_attributable_count,
        ambiguous_count=ambiguous_count,
        no_provenance_count=no_provenance_count,
        attributions=attributions,
    )


def apply_listing_attribution(session: Session, attribution: ListingAttributionResult) -> int:
    """Writes every `ListingAttribution` in `attribution.attributions` -
    only ever the unambiguous ones `compute_listing_attribution` already
    decided on; this function makes no judgment calls of its own. Flushes,
    does not commit."""
    if not attribution.attributions:
        return 0
    listing_ids = [a.listing_id for a in attribution.attributions]
    rows_by_id = {
        row.id: row
        for row in session.execute(select(DiscoveredListing).where(DiscoveredListing.id.in_(listing_ids)))
        .scalars()
        .all()
    }
    applied = 0
    for attr in attribution.attributions:
        row = rows_by_id.get(attr.listing_id)
        if row is not None and row.discovered_by_saved_search_id is None:
            row.discovered_by_saved_search_id = attr.saved_search_id
            applied += 1
    session.flush()
    return applied


def run_cutover(
    session: Session,
    *,
    email: str,
    password: str | None,
    apply: bool,
) -> BootstrapReport:
    """The full cutover in one call: find-or-create the bootstrap user,
    backfill unowned saved searches, compute (and, if `apply`, write)
    historical listing attribution.

    `password` is only ever used, and only ever required, when the
    account doesn't already exist *and* `apply` is `True` - a dry run
    never needs one (nothing is written), and a rerun that finds an
    existing account never needs one either (see this module's docstring
    on idempotency). Passing `None` when a password is actually required
    raises `ValueError` before anything is touched.

    Two commit points, not one, and this is deliberate (see this module's
    docstring for the full reasoning): user creation (if new) commits on
    its own the moment it succeeds; the saved-search/listing backfill is
    a second, separate atomic unit, committed together or not at all. A
    rerun after a failure in the second half simply finds the
    already-created user and retries the backfill - no user-creation
    work is ever redone or duplicated.
    """
    user = find_bootstrap_user(session, email)
    user_already_existed = user is not None

    if user is None and apply:
        if password is None:
            raise ValueError("A password is required to create a new bootstrap account")
        user = create_bootstrap_user(session, email=email, password=password)
        session.commit()

    unowned_saved_searches = list_unowned_saved_searches(session)
    backfill_result = SavedSearchBackfillResult(unowned_before=len(unowned_saved_searches), assigned_count=0)

    # Candidates for listing attribution: every saved search that is, or
    # (in apply mode) is about to become, bootstrap-owned. In dry-run
    # mode with no existing user, this is exactly the unowned set - see
    # compute_listing_attribution's docstring for why no real user id is
    # needed to compute this.
    candidate_ids = [s.id for s in unowned_saved_searches]
    if user is not None:
        already_owned = session.execute(
            select(SavedSearch.id).where(SavedSearch.user_id == user.id)
        ).scalars().all()
        candidate_ids = list({*candidate_ids, *already_owned})

    listing_attribution = compute_listing_attribution(session, candidate_saved_search_ids=candidate_ids)

    if apply and user is not None:
        backfill_result.assigned_count = assign_saved_searches_to_user(session, unowned_saved_searches, user.id)
        apply_listing_attribution(session, listing_attribution)
        session.commit()

    return BootstrapReport(
        email=email,
        user_id=user.id if user is not None else None,
        user_already_existed=user_already_existed,
        saved_search_backfill=backfill_result,
        listing_attribution=listing_attribution,
        applied=apply,
    )
